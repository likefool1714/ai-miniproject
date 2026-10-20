"""실제 웹 프록시·FastAPI·stdio MCP로 5단계 복구 흐름을 대화별 3회 확인한다."""
import json
import statistics
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results/recovery-2026-10-20"
EXPECTED = [("ongoing", False), ("partial", False), ("partial", False), ("recovered", False), ("ongoing", True)]


def main():
    records = []
    with httpx.Client(base_url="http://127.0.0.1:8600", timeout=130) as client:
        health = client.get("/health").json()
        assert health.get("monitoring_mode") == "recovery-demo", "recovery-demo 시나리오 서버가 필요합니다."
        for repetition in range(1, 4):
            thread = "web-probe-" + uuid4().hex[:12]
            for index, (phase, recurrence) in enumerate(EXPECTED):
                message = "payments-service 복구 재점검" if index == 0 else "그 서비스 복구됐어? 다시 확인해줘"
                started = time.perf_counter()
                response = client.post("/chat", json={"message": message, "thread_id": thread})
                elapsed = time.perf_counter() - started
                response.raise_for_status()
                body = response.json()
                item = body["metadata"]["recovery_results"][0]
                a = item["assessment"]
                assert a["phase"] == phase and a["recurrence"] == recurrence, body
                records.append({"repetition": repetition, "step": index+1, "input": message,
                                "elapsed_seconds": elapsed, "reply": body["reply"], "result": item,
                                "retry_count": body["metadata"]["retry_count"]})
            history = client.get("/observations", params={"thread_id": thread, "system_name": "payments-service"}).json()
            assert len(history["observations"]) == 5
            report = client.get("/reports/markdown", params={"thread_id": thread, "system_name": "payments-service"})
            report.raise_for_status()
            assert "장애 재발" in report.text
            (OUT / f"web-report-{repetition}.md").write_text(report.text)
    summary = {"mode": health, "correct": len(records), "total": 15, "median_seconds": statistics.median(r["elapsed_seconds"] for r in records),
               "scope": "actual HTTP proxy + FastAPI + stdio MCP, synthetic recovery-demo observations, recovery route performs no LLM call"}
    (OUT / "web-responses.json").write_text(json.dumps(records, ensure_ascii=False, indent=2))
    (OUT / "web-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
