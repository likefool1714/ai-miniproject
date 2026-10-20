"""로컬 모델 작성 방식 비교와 실제 웹 전체 흐름 재검증. PDF/PPT를 만들지 않는다."""
import argparse
import ast
import asyncio
import inspect
import json
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from agent.grounding import build_bundle, fallback_draft, render_answer, validate_draft
from agent.ollama_llm import OllamaQualityLLM

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results/grounding-2026-10-20"
BASELINE = "3bb34a9"


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))


async def writer_probe(repetitions):
    # .write는 비교용 원본으로 남겨 두었다. 이전 커밋과 함수 AST가 같은지 확인한다.
    old_source = subprocess.check_output(["git", "show", f"{BASELINE}:agent/ollama_llm.py"], cwd=ROOT, text=True)
    old_class = next(n for n in ast.parse(old_source).body if isinstance(n, ast.ClassDef) and n.name == "OllamaQualityLLM")
    old_write = next(n for n in old_class.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "write")
    current = ast.parse(inspect.getsource(inspect.getmodule(OllamaQualityLLM)))
    current_class = next(n for n in current.body if isinstance(n, ast.ClassDef) and n.name == "OllamaQualityLLM")
    current_write = next(n for n in current_class.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "write")
    assert ast.dump(old_write, include_attributes=False) == ast.dump(current_write, include_attributes=False)
    records = []
    query = "payments-service 장애 원인과 CPU 런북 조건을 진단해주세요."
    for repetition in range(1, repetitions + 1):
        observation = {"system_name": "payments-service", "status": "degraded", "cpu_percent": 89.5,
                       "memory_percent": 55.0, "alerts": ["CPU high (threshold 80%)"],
                       "last_checked": datetime.now(timezone.utc).isoformat(),
                       "sample_id": f"writer-fixed-{repetition}", "data_source": "controlled-writer-fixture"}
        collected = [{"source": "monitoring", "data": observation},
                     {"source": "runbook", "target": "cpu-spike",
                      "data": (ROOT / "data/runbooks/cpu-spike.md").read_text()}]
        model = OllamaQualityLLM("qwen3:8b", 0.002)
        started = time.perf_counter()
        try:
            text, usage = await asyncio.wait_for(model.write(query, collected, 1, None), timeout=60)
            before = {"reply": text, "usage": usage, "completed": True}
        except Exception as exc:
            before = {"completed": False, "error": type(exc).__name__}
        before["elapsed_seconds"] = time.perf_counter() - started
        bundle = build_bundle(collected, ["payments-service"])
        attempts, draft = [], None
        feedback, error = None, None
        started = time.perf_counter()
        for attempt in range(1, 3):
            try:
                raw, usage = await asyncio.wait_for(model.write_grounded(query, bundle, feedback), timeout=40)
                draft, errors = validate_draft(raw, bundle)
                attempts.append({"attempt": attempt, "raw": raw, "errors": errors, "usage": usage})
                if draft:
                    break
                feedback = ", ".join(errors)
            except Exception as exc:
                error = type(exc).__name__
                attempts.append({"attempt": attempt, "error": error})
                break
        status = "validated" if draft else "fallback"
        reply = render_answer(bundle, draft or fallback_draft(bundle), status)
        assert "CPU 89.5% > 90% → 불충족" in reply
        assert "최근 로그를 분석했다" not in reply
        after = {"status": status, "reply": reply, "attempts": attempts,
                 "elapsed_seconds": time.perf_counter() - started}
        records.append({"repetition": repetition, "query": query, "collected": collected, "before": before, "after": after})
        save("writer-responses.json", records)
        print(json.dumps({"repetition": repetition, "before_completed": before["completed"],
                          "before_seconds": before["elapsed_seconds"], "after_status": status,
                          "after_seconds": after["elapsed_seconds"]}), flush=True)
    summary = {"status": "passed", "scope": "same query/observation/runbook/model; writer-stage comparison only; synthetic fixed data",
               "baseline_commit": BASELINE, "baseline_write_ast_unchanged": True,
               "model": "qwen3:8b", "attempted_per_version": repetitions,
               "before_generation": "free prose; thinking=model default; evaluator timeout=60s",
               "after_generation": "JSON schema selection; think=false; num_predict=1024; timeout=40s; rewrite<=1",
               "before_completed": sum(r["before"]["completed"] for r in records),
               "after_validated": sum(r["after"]["status"] == "validated" for r in records),
               "after_fallback": sum(r["after"]["status"] == "fallback" for r in records),
               "before_median_seconds": statistics.median(r["before"]["elapsed_seconds"] for r in records),
               "after_median_seconds": statistics.median(r["after"]["elapsed_seconds"] for r in records),
               "limits": "before prose requires manual claim review; this does not measure general diagnostic accuracy or production recovery"}
    save("writer-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def web_probe(repetitions):
    records = []
    expected = [("ongoing", False), ("partial", False), ("partial", False), ("recovered", False), ("ongoing", True)]
    with httpx.Client(base_url="http://127.0.0.1:8600", timeout=125) as client:
        health = client.get("/health").json()
        assert health["llm_backend"] == "ollama" and health["monitoring_mode"] == "recovery-demo"
        for repetition in range(1, repetitions + 1):
            thread = "grounding-web-" + uuid4().hex[:12]
            turns = []
            for step, (phase, recurrence) in enumerate(expected):
                message = "payments-service 장애를 진단해주세요" if step == 0 else "그 서비스 복구됐어? 다시 확인해줘"
                started = time.perf_counter()
                response = client.post("/chat", json={"message": message, "thread_id": thread})
                response.raise_for_status()
                body = response.json()
                assessment = body["metadata"]["recovery_results"][0]["assessment"]
                assert assessment["phase"] == phase and assessment["recurrence"] == recurrence
                assert body["metadata"]["recovery_results"][0]["snapshot"]["system_name"] == "payments-service"
                if step == 0:
                    assert body["metadata"]["validation_status"] == "validated", body
                    assert "CPU 95%" in body["reply"] and "M1" in body["reply"]
                    assert "미실행" in body["reply"]
                turns.append({"input": message, "elapsed_seconds": time.perf_counter() - started, "body": body})
            history = client.post("/chat", json={"message": "조사 이력 보여주세요", "thread_id": thread}).json()
            report = client.post("/chat", json={"message": "보고서 내보내기", "thread_id": thread}).json()
            url = report["metadata"]["reports"][0]["url"]
            downloaded = client.get(url)
            downloaded.raise_for_status()
            assert "장애 재발" in downloaded.text
            rows = client.get("/observations", params={"thread_id": thread, "system_name": "payments-service"}).json()["observations"]
            assert len(rows) == 5
            records.append({"repetition": repetition, "thread": thread, "turns": turns,
                            "history_reply": history["reply"], "report": downloaded.text, "history_count": len(rows)})
            save("web-responses.json", records)
            print(json.dumps({"repetition": repetition, "five_stages": "passed", "initial_seconds": turns[0]["elapsed_seconds"]}), flush=True)
        extras = []
        for message in ["payments-service와 cache-service 장애를 함께 진단해주세요", "web-01 상태를 진단해주세요"]:
            started = time.perf_counter()
            response = client.post("/chat", json={"message": message, "thread_id": "grounding-extra-" + uuid4().hex[:12]})
            response.raise_for_status()
            body = response.json()
            assert body["metadata"]["validation_status"] == "validated", body
            if "web-01" in message:
                assert not [e for e in body["events"] if e.get("source") == "runbook"]
            else:
                assert {r["snapshot"]["system_name"] for r in body["metadata"]["recovery_results"]} == {"payments-service", "cache-service"}
            extras.append({"input": message, "elapsed_seconds": time.perf_counter() - started, "body": body})
        stream = client.get("/chat/stream", params={"message": "payments-service 장애 진단", "thread_id": "grounding-sse-" + uuid4().hex[:12]})
        stream.raise_for_status()
        events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: {")]
        assert not [e for e in events if e["type"] == "AnswerToken"]
        final = next(e for e in events if e["type"] == "FinalAnswer")
        assert final["validation_status"] == "validated"
        save("web-extra-responses.json", {"extras": extras, "sse": events})
    summary = {"status": "passed", "scope": "actual HTTP proxy + FastAPI + stdio MCP + Ollama; synthetic recovery-demo data",
               "fast_generation": "think=false; num_predict=512; transport timeout=20s",
               "health": health, "full_lifecycle_repetitions": repetitions, "lifecycle_checks": repetitions * 5,
               "initial_diagnosis_validated": repetitions, "extra_diagnoses_validated": len(extras),
               "sse_diagnosis_validated": True, "sse_unvalidated_tokens": 0,
               "initial_diagnosis_median_seconds": statistics.median(r["turns"][0]["elapsed_seconds"] for r in records)}
    save("web-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["writer", "web"])
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    assert 1 <= args.repeat <= 10
    save(f"{args.mode}-summary.json", {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
    try:
        asyncio.run(writer_probe(args.repeat)) if args.mode == "writer" else web_probe(args.repeat)
    except Exception as exc:
        save(f"{args.mode}-summary.json", {"status": "failed", "error": type(exc).__name__, "detail": str(exc)[:300]})
        raise
