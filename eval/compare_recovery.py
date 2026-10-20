"""같은 관측·질문으로 원본 커밋/현재 그래프를 비교한다. LLM API 과금 없이 Ollama 사용 가능.

외부 I/O는 고정 MCP fixture로 통제한다. 실제 stdio MCP·HTTP 연결은 tests/test_recovery_api.py가 검증한다.
원본은 구조화 복구 판정이 없으므로 답변 원문을 보존하고 의미 판정은 수동 검토한다.
"""
import argparse
import asyncio
import importlib.util
import io
import json
import os
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.recovery_store import RecoveryStore
from agent.session_store import SqliteSessionStore


class FixedMCP:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    async def call_tool(self, name, arguments, **kw):
        self.calls.append({"tool": name, **arguments})
        if arguments["system_name"] != self.snapshot["system_name"]:
            raise ValueError("평가에 없는 서비스 조회")
        return deepcopy(self.snapshot)

    async def find_prompt(self, name):
        return "ops"

    async def get_prompt(self, name, arguments, **kw):
        return f"""다음 장애에 대해 표준 진단 절차를 수행하세요.

## 장애 정보
- 시스템: {arguments['system_name']}
- 증상: {arguments['symptom']}

## 진단 절차 (반드시 순서대로 수행)
1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.
2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.
3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.
"""

    async def find_resource_template(self, name):
        return "ops", "runbook://incident-response/{incident_type}"

    async def read_resource(self, uri, **kw):
        return (ROOT / "data/runbooks" / (uri.rsplit("/", 1)[-1] + ".md")).read_text()


def load_original(directory, commit):
    archive = subprocess.check_output(["git", "archive", commit], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        package.extractall(directory, filter="data")
    spec = importlib.util.spec_from_file_location("baseline_agent", directory / "agent/__init__.py", submodule_search_locations=[str(directory / "agent")])
    module = importlib.util.module_from_spec(spec)
    sys.modules["baseline_agent"] = module
    spec.loader.exec_module(module)
    return __import__("baseline_agent.graph", fromlist=["build_graph"])


async def main(args):
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    current_time = datetime.now(timezone.utc)
    steps = json.loads((ROOT / "data/scenarios/recovery-demo.json").read_text())["payments-service"]
    expected = [("ongoing", False), ("partial", False), ("partial", False), ("recovered", False), ("ongoing", True)]
    query = "payments-service의 복구 여부를 진단해 주세요"
    if args.backend == "ollama":
        from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM("qwen3:0.6b", 0.00015)
        quality = OllamaQualityLLM("qwen3:8b", 0.005)
    else:
        fast, quality = FastLLM("fast-model-stub", 0.00015), QualityLLM("quality-model-stub", 0.005)
    results = []
    with tempfile.TemporaryDirectory(prefix="recovery-comparison-") as temporary:
        directory = Path(temporary)
        original = load_original(directory / "baseline", args.baseline_commit)
        for repetition in range(1, args.repetitions + 1):
            for version in ("before", "after"):
                db = directory / f"{version}-{repetition}.db"
                session = SqliteSessionStore(db)
                store = RecoveryStore(db, clock=lambda: current_time) if version == "after" else None
                history = []
                for index, (step, truth) in enumerate(zip(steps, expected)):
                    snapshot = {**step, "system_name": "payments-service", "last_checked": (current_time - timedelta(seconds=(len(steps)-1-index)*10)).isoformat(),
                                "sample_id": f"fixed:{repetition}:{index}", "data_source": "frozen-evaluation-fixture"}
                    mcp = FixedMCP(snapshot)
                    deps = SupervisorDeps(fast, quality, mcp, session, "v2", recovery_store=store)
                    graph = (original.build_graph if version == "before" else build_graph)(deps)
                    state = {"query": query, "thread_id": f"eval-{repetition}", "history": history,
                             "events": [], "cost_usd": 0.0}
                    started = time.perf_counter()
                    try:
                        answer = await asyncio.wait_for(graph.ainvoke(state), timeout=120)
                        elapsed = time.perf_counter() - started
                        assessment = (answer.get("recovery_results") or [{}])[0].get("assessment")
                        row = {"version": version, "repetition": repetition, "step": index + 1,
                               "input": query, "snapshot": snapshot, "expected_phase": truth[0], "expected_recurrence": truth[1],
                               "assessment": assessment, "reply": answer["final"], "elapsed_seconds": elapsed,
                               "tool_calls": mcp.calls, "estimated_cost_usd": answer.get("cost_usd", 0),
                               "correct": assessment["phase"] == truth[0] and assessment["recurrence"] == truth[1] if assessment else None,
                               "manual_review": None, "status": "completed"}
                        history += [{"role": "user", "content": query}, {"role": "assistant", "content": answer["final"]}]
                    except Exception as exc:
                        row = {"version": version, "repetition": repetition, "step": index + 1,
                               "status": "error", "error_type": type(exc).__name__, "elapsed_seconds": time.perf_counter() - started,
                               "correct": False if version == "after" else None}
                    results.append(row)
                    (output / "responses.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
                    print(f"{version} repeat={repetition} step={index+1} {row['status']} {row['elapsed_seconds']:.3f}s", flush=True)
                session.close()
                if store:
                    (output / f"sample-report-{repetition}.md").write_text(store.report(f"eval-{repetition}", "payments-service") or "")
                    store.close()
    summary = {"baseline_commit": args.baseline_commit, "backend": args.backend, "repetitions": args.repetitions,
               "fixed_clock": current_time.isoformat(), "io": "injected fixed MCP fixture; no network MCP in this comparison",
               "query": query, "notes": ["Before 자연어의 의미 판정은 manual_review 항목으로 별도 검토하며 구조화 판정 부재를 오답으로 계산하지 않습니다.", "cost_usd is a virtual-price estimate, not an API bill.", "After recovery route uses deterministic policy without LLM calls."]}
    for version in ("before", "after"):
        rows = [r for r in results if r["version"] == version]
        summary[version] = {"completed": sum(r["status"] == "completed" for r in rows), "total": len(rows),
                            "median_seconds": statistics.median(r["elapsed_seconds"] for r in rows),
                            "structured_verdicts": sum(r.get("assessment") is not None for r in rows)}
        if version == "after":
            summary[version]["correct"] = sum(r["correct"] is True for r in rows)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["fake", "ollama"], default="ollama")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--baseline-commit", default="d1043ff")
    parser.add_argument("--output", default="results/recovery-2026-10-20")
    asyncio.run(main(parser.parse_args()))
