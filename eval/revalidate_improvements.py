"""등록 기능과 설명 보완을 실제 로컬 경로에서 재검증한다. 기존 결과는 덮어쓰지 않는다."""
import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

import httpx

from eval import probe_grounding, probe_service_coverage
from eval.compare_recovery import FixedMCP
from agent.deps import SupervisorDeps
from agent.graph import build_graph
from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
from agent.recovery_store import RecoveryStore
from agent.session_store import SqliteSessionStore

ROOT = Path(__file__).resolve().parent.parent


async def fixed_cpu(out):
    now = datetime.now(timezone.utc)
    sample = {"system_name": "payments-service", "status": "degraded", "cpu_percent": 89.5,
              "memory_percent": 55, "alerts": ["CPU high (threshold 80%)"],
              "last_checked": now.isoformat(), "sample_id": "revalidation-cpu-89.5",
              "data_source": "controlled-revalidation-fixture"}
    with tempfile.TemporaryDirectory(prefix="explanation-revalidation-") as temporary:
        path = Path(temporary) / "fixed.db"
        session, store = SqliteSessionStore(path), RecoveryStore(path, clock=lambda: now)
        mcp = FixedMCP(sample)
        deps = SupervisorDeps(OllamaFastLLM("qwen3:0.6b", 0.00015),
                              OllamaQualityLLM("qwen3:8b", 0.005), mcp, session, "v2", recovery_store=store)
        query = "payments-service 장애 원인과 CPU 런북 조건을 진단해주세요."
        try:
            answer = await asyncio.wait_for(build_graph(deps).ainvoke(
                {"query": query, "thread_id": "fixed-cpu", "history": [], "events": [], "cost_usd": 0}), 120)
            (out / "fixed-cpu-response.json").write_text(json.dumps(
                {"query": query, "snapshot": sample, "answer": answer, "tool_calls": mcp.calls,
                 "scope": "actual Ollama graph with injected MCP input; not stdio MCP or HTTP"}, ensure_ascii=False, indent=2))
            assert answer["validation_status"] == "validated"
            text = answer["final"]
            assert "CPU 부하를 우선 점검할 근거" in text and "메모리 55%는 정상 기준 안" in text
            assert "반복 호출, 배치 작업 중복" in text
            assert "CPU 89.5% > 90% → 불충족" in text
            assert "이 수치만으로는 런북의 증설 조건을 충족하지 않습니다" in text
            assert "원인으로 단정하지 않습니다" in text
            assert "로그를 분석했다" not in text
        finally:
            store.close()
            session.close()


def clarification_and_duplicate(out):
    records = []
    def save():
        (out / "clarification-and-duplicate.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
    with httpx.Client(base_url="http://127.0.0.1:8600", timeout=130) as client:
        for message in ["서비스가 느려졌어요. 장애를 진단해주세요", "unregistered-service 장애를 진단해주세요"]:
            response = client.post("/chat", json={"message": message, "thread_id": "clarify-" + uuid4().hex[:10]})
            response.raise_for_status()
            body = response.json()
            records.append({"query": message, "body": body})
            save()
            assert "어떤 서비스를 확인할까요" in body["reply"]
            assert not body["metadata"]["recovery_results"]
            assert not any(e["type"] == "TaskCompleted" for e in body["events"])
        thread = "duplicate-" + uuid4().hex[:10]
        bodies = []
        for _ in range(6):
            response = client.post("/chat", json={"message": "payments-service 복구 재점검", "thread_id": thread})
            response.raise_for_status()
            bodies.append(response.json())
        observations = client.get("/observations", params={"thread_id": thread, "system_name": "payments-service"}).json()
        records.append({"scenario": "final sample requery does not add observation", "bodies": bodies, "observations": observations})
        save()
        assert len(observations["observations"]) == 5
        assert bodies[-1]["metadata"]["recovery_results"][0]["duplicate"] is True
        assert bodies[-1]["metadata"]["recovery_results"][0]["assessment"]["phase"] == "ongoing"


def main(args):
    out = ROOT / args.output
    out.mkdir(parents=True, exist_ok=True)
    result = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
              "product_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "components": {}, "scope": "local Ollama + HTTP proxy/FastAPI + stdio MCP; synthetic monitoring; fixed CPU comparison uses injected MCP"}
    def save():
        (out / "live-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    save()
    probe_grounding.OUT = probe_service_coverage.OUT = out
    for name, run in [
        ("three_lifecycles_and_multi_healthy_sse", lambda: probe_grounding.web_probe(3)),
        ("other_services_and_context_switches", probe_service_coverage.main),
        ("fixed_cpu_interpretation_and_action", lambda: asyncio.run(fixed_cpu(out))),
        ("clarification_and_duplicate", lambda: clarification_and_duplicate(out)),
    ]:
        try:
            run()
            result["components"][name] = {"status": "passed"}
        except Exception as exc:
            result["components"][name] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
            traceback.print_exc()
        save()
        print(name, result["components"][name]["status"], flush=True)
    result["status"] = "passed" if all(v["status"] == "passed" for v in result["components"].values()) else "failed"
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    save()
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="results/revalidation-2026-10-21")
    main(parser.parse_args())
