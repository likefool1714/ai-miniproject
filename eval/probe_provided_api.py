"""제공 API의 실제 웹 진단·복구 호환 검사. 인증 정보는 저장하지 않는다."""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx


def run(output: Path, base_url: str):
    output.mkdir(parents=True, exist_ok=True)
    records = []
    summary = {
        "status": "running", "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "real remote model API + HTTP proxy + FastAPI + stdio MCP; synthetic monitoring",
        "limits": "One lifecycle and selected compatibility cases, not an accuracy benchmark. Recovery/history/report do not call an LLM. cost_usd is a virtual-price estimate.",
    }

    def save():
        (output / "web-responses.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
        (output / "web-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    save()
    try:
        with httpx.Client(base_url=base_url, timeout=135) as client:
            response = client.get("/health")
            response.raise_for_status()
            health = response.json()
            summary["health"] = health
            assert health["llm_backend"] == "openai", "backend must be openai"
            assert health["supervisor_version"] == "v2", "version must be v2"
            assert health["monitoring_mode"] == "recovery-demo", "scenario must be recovery-demo"

            def chat(message, thread):
                started = time.perf_counter()
                res = client.post("/chat", json={"message": message, "thread_id": thread})
                body = res.json()
                records.append({"message": message, "thread_id": thread, "http_status": res.status_code,
                                "elapsed_seconds": time.perf_counter() - started, "body": body})
                save()
                res.raise_for_status()
                return body

            thread = "provided-api-" + uuid4().hex[:12]
            expected = [("ongoing", False), ("partial", False), ("partial", False),
                        ("recovered", False), ("ongoing", True)]
            for step, (phase, recurrence) in enumerate(expected):
                message = "payments-service 장애를 진단해주세요" if step == 0 else "그 서비스 복구됐어? 다시 확인해줘"
                body = chat(message, thread)
                result = body["metadata"]["recovery_results"][0]
                assert result["snapshot"]["system_name"] == "payments-service", "target mismatch"
                assert result["assessment"]["phase"] == phase, "phase mismatch"
                assert result["assessment"]["recurrence"] is recurrence, "recurrence mismatch"
                if step == 0:
                    assert body["metadata"]["validation_status"] == "validated", "initial diagnosis not validated"
                    assert "CPU 95%" in body["reply"] and "메모리 90%" in body["reply"], "facts missing"
                    summary["initial_diagnosis_seconds"] = records[-1]["elapsed_seconds"]
                summary["lifecycle_checks"] = step + 1
                save()
                print(json.dumps({"lifecycle_step": step + 1, "phase": phase, "recurrence": recurrence}), flush=True)

            chat("조사 이력 보여주세요", thread)
            report = chat("보고서 내보내기", thread)
            report_url = report["metadata"]["reports"][0]["url"]
            downloaded = client.get(report_url)
            downloaded.raise_for_status()
            assert "장애 재발" in downloaded.text, "report recurrence missing"
            (output / "report.md").write_text(downloaded.text)
            rows_response = client.get("/observations", params={"thread_id": thread, "system_name": "payments-service"})
            rows_response.raise_for_status()
            rows = rows_response.json()["observations"]
            assert len(rows) == 5, "history/report changed observation count"
            summary["history_count"] = len(rows)
            summary["report_download"] = "passed"
            summary["extra_diagnoses"] = []
            for message, targets in [
                ("payments-service와 cache-service 장애를 함께 진단해주세요", {"payments-service", "cache-service"}),
                ("web-01 상태를 진단해주세요", {"web-01"}),
            ]:
                body = chat(message, "provided-extra-" + uuid4().hex[:12])
                assert body["metadata"]["validation_status"] == "validated", "extra diagnosis not validated"
                actual = {r["snapshot"]["system_name"] for r in body["metadata"]["recovery_results"]}
                assert actual == targets, "extra targets mismatch"
                summary["extra_diagnoses"].append({"targets": sorted(targets), "status": "validated"})
                save()

            res = client.get("/chat/stream", params={"message": "payments-service 장애를 진단해주세요", "thread_id": "provided-sse-" + uuid4().hex[:12]})
            res.raise_for_status()
            events = [json.loads(line[6:]) for line in res.text.splitlines() if line.startswith("data: {")]
            (output / "sse-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
            final = next(e for e in events if e["type"] == "FinalAnswer")
            assert final["validation_status"] == "validated", "SSE diagnosis not validated"
            assert not [e for e in events if e["type"] == "AnswerToken"], "unvalidated token emitted"
            summary.update(status="passed", post_diagnoses_validated=3, sse_diagnoses_validated=1,
                           sse_unvalidated_tokens=0, finished_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        # 예외 문자열·HTTP 헤더는 저장하지 않아 인증 정보의 혼입을 피한다.
        summary.update(status="failed", error_type=type(exc).__name__)
        save()
        raise
    save()
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("results/api-2026-10-21"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8600")
    args = parser.parse_args()
    run(args.output, args.base_url)
