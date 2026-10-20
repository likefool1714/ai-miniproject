"""Cross-check isolated rules and supplied dependencies without inference."""
import asyncio
import json
import tempfile
from pathlib import Path

from agent.graph import _classify_incident_type, validate_node
from agent.ollama_llm import OllamaFastLLM
from agent.retry import with_retry
from agent.session_store import SqliteSessionStore


async def main():
    rows = []
    for round_number in range(1, 4):
        def classify(alerts, status="degraded"):
            return _classify_incident_type([{"source": "monitoring", "data": {
                "status": status, "alerts": alerts}}])

        model = OllamaFastLLM("not-called", 0)
        usage = {"usd": 0, "tokens": 0}
        model._chat = lambda *args, **kwargs: (json.dumps({"needs_analysis": False, "tasks": [
            {"agent": "research", "target": "billing-service"}]}), usage)
        unknown_plan, _ = model.plan("billing-service 응답지연 원인 진단", "test")
        model._chat = lambda *args, **kwargs: (json.dumps({"needs_analysis": True, "tasks": [
            {"agent": "research", "target": "payments-service"}]}), usage)
        incomplete_plan, _ = model.plan("payments-service와 cache-service 모두 진단", "test")
        wrong_facts = await validate_node({"draft": "1) 실제 수집 결과와 무관하게 CPU가 999%라고 단정합니다.",
                                          "events": [], "validation_attempts": 0}, None)
        short_final = await validate_node({"draft": "몰라요", "events": [], "validation_attempts": 2}, None)
        attempts = 0

        async def flaky():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncio.TimeoutError("controlled transient failure")
            return "ok"

        retry_value, retries = await with_retry(flaky, base_delay=0)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.db"
            store = SqliteSessionStore(path)
            store.append("probe", "user", "세션 저장 검증")
            store.close()
            reopened = SqliteSessionStore(path)
            persisted = reopened.load("probe")
            reopened.close()

        rows.append({
            "round": round_number,
            "single_cpu": classify(["CPU 91% (threshold 80%)"]),
            "single_memory": classify(["memory usage climbing steadily over 6h", "heap near limit"]),
            "single_network": classify(["upstream timeout to payments-service (504)", "network packet loss 3%"]),
            "mixed_cpu_memory": classify(["CPU 91%", "memory usage climbing steadily over 6h"]),
            "healthy_no_alerts": classify([], status="healthy"),
            "korean_cpu": classify(["프로세서 사용률이 95%로 급증"]),
            "unknown_normalized_targets": [t.get("target") for t in unknown_plan["tasks"]],
            "incomplete_plan_targets": [t.get("target") for t in incomplete_plan["tasks"]],
            "invented_facts_validation": wrong_facts["events"][-1],
            "third_short_draft_validation": short_final["events"][-1],
            "retry_recovered": retry_value == "ok" and retries == 1,
            "sqlite_reopen_persisted": persisted == [{"role": "user", "content": "세션 저장 검증"}],
        })
    output = Path("results/baseline-2026-10-20/rule-crosscheck.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
