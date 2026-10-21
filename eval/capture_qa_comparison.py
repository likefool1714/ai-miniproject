"""원본/현재 그래프의 실제 Ollama 답변을 저장한다. 비교 입력만 fixture로 고정한다.

실행: .venv/bin/python -m eval.capture_qa_comparison
스크린샷용 HTML은 별도 읽기 전용 뷰이며 제품 UI와 구별한다.
"""
import argparse
import asyncio
import importlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from eval.compare_recovery import FixedMCP, ROOT, load_original
from agent.deps import SupervisorDeps
from agent.graph import build_graph
from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
from agent.recovery_store import RecoveryStore
from agent.session_store import SqliteSessionStore

OUT = ROOT / "results/qa-comparison-2026-10-21"


async def main(baseline_commit="d1043ff"):
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    meta = {"status": "running", "started_at": now.isoformat(),
            "before_commit": subprocess.check_output(["git", "rev-parse", baseline_commit], cwd=ROOT, text=True).strip(),
            "after_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "fast_model": "qwen3:0.6b", "quality_model": "qwen3:8b",
            "io": "Injected fixed MCP input; real local Ollama. Not stdio MCP/HTTP end-to-end timing.",
            "limits": ["1 execution per question/version; not an accuracy benchmark.",
                       "Original graph AND original Ollama adapter loaded from before_commit.",
                       "After recovery/clarification routes are code decisions without LLM generation.",
                       "HTML comparison viewer displays saved replies; it is not the original product UI."]}
    rows = []

    def save():
        (OUT / "responses.json").write_text(json.dumps({"metadata": meta, "rows": rows}, ensure_ascii=False, indent=2))

    def snapshot(system, status, cpu, memory, alerts, sample, age=0):
        return {"system_name": system, "status": status, "cpu_percent": cpu,
                "memory_percent": memory, "alerts": alerts,
                "last_checked": (now - timedelta(seconds=age)).isoformat(),
                "sample_id": sample, "data_source": "controlled-comparison-fixture"}

    cases = [
        ("target", "조사 대상 확인", "서비스가 느려졌어요. 장애를 진단해주세요.", [
            snapshot("auth-service", "degraded", 78, 61, ["p95 latency 2.4s (threshold 1.0s)", "connection pool 92% used"], "target-1")]),
        ("cpu", "런북 조건 확인", "payments-service 장애 원인과 CPU 런북 조건을 진단해주세요.", [
            snapshot("payments-service", "degraded", 89.5, 55, ["CPU high (threshold 80%)"], "cpu-1")]),
        ("recovery", "일시적 정상화 확인", "payments-service의 복구 여부를 진단해 주세요.", [
            snapshot("payments-service", "degraded", 95, 90, ["CPU high (threshold 80%)", "memory high"], "recovery-1", 10),
            snapshot("payments-service", "healthy", 55, 60, [], "recovery-2")]),
    ]
    save()
    with tempfile.TemporaryDirectory(prefix="qa-comparison-") as directory:
        directory = Path(directory)
        original = load_original(directory / "baseline", baseline_commit)
        old_llm = importlib.import_module("baseline_agent.ollama_llm")
        old_store = (importlib.import_module("baseline_agent.recovery_store").RecoveryStore
                     if (directory / "baseline/agent/recovery_store.py").exists() else None)
        for case_id, title, query, snapshots in cases:
            for version in ("before", "after"):
                db = directory / f"{case_id}-{version}.db"
                session = SqliteSessionStore(db)
                store_class = RecoveryStore if version == "after" else old_store
                store = store_class(db, clock=lambda: now) if store_class else None
                provider = old_llm if version == "before" else None
                fast = (provider.OllamaFastLLM if provider else OllamaFastLLM)("qwen3:0.6b", 0.00015)
                quality = (provider.OllamaQualityLLM if provider else OllamaQualityLLM)("qwen3:8b", 0.005)
                history = []
                try:
                    for step, observation in enumerate(snapshots, 1):
                        mcp = FixedMCP(observation)
                        deps = SupervisorDeps(fast, quality, mcp, session, "v2", recovery_store=store)
                        graph = (original.build_graph if version == "before" else build_graph)(deps)
                        started = time.perf_counter()
                        row = {"case": case_id, "title": title, "version": version, "step": step,
                               "query": query, "snapshot": observation}
                        try:
                            answer = await asyncio.wait_for(graph.ainvoke({"query": query, "thread_id": case_id,
                                "history": history, "events": [], "cost_usd": 0.0}), timeout=120)
                            row.update(status="completed", reply=answer["final"], events=answer.get("events", []),
                                       recovery_results=answer.get("recovery_results", []),
                                       validation_status=answer.get("validation_status"), collected=answer.get("collected", []))
                            history += [{"role": "user", "content": query}, {"role": "assistant", "content": answer["final"]}]
                        except Exception as exc:
                            row.update(status="error", error_type=type(exc).__name__, error=str(exc)[:300])
                        row.update(elapsed_seconds=time.perf_counter() - started, tool_calls=mcp.calls)
                        rows.append(row)
                        save()
                        print(f"{case_id} {version} step={step}: {row['status']} {row['elapsed_seconds']:.2f}s", flush=True)
                finally:
                    session.close()
                    if store:
                        store.close()
    meta["status"] = "completed" if all(r["status"] == "completed" for r in rows) else "has_errors"
    meta["completed_at"] = datetime.now(timezone.utc).isoformat()
    save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-commit", default="d1043ff")
    parser.add_argument("--output", default="results/qa-comparison-2026-10-21")
    args = parser.parse_args()
    OUT = ROOT / args.output
    asyncio.run(main(args.baseline_commit))
