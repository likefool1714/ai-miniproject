"""서로 다른 서비스·한국어 별칭·대상 변경 후 대명사 복원을 실제 HTTP로 확인한다."""
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

OUT = Path(__file__).resolve().parent.parent / "results/professor-audit-2026-10-20"


def main():
    records = []
    thread = "coverage-" + uuid4().hex[:12]
    status = "running"
    def save(error=None):
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "service-coverage.json").write_text(json.dumps(
            {"status": status, "scope": "actual Ollama + HTTP + MCP; synthetic sample-json-jitter data",
             "records": records, "error": error}, ensure_ascii=False, indent=2))
    save()
    try:
        with httpx.Client(base_url="http://127.0.0.1:8600", timeout=140) as client:
            for query, target, runbook in [
                ("auth-service 장애를 진단해주세요", "auth-service", "high-latency"),
                ("gateway-service 장애를 진단해주세요", "gateway-service", "network-timeout"),
                ("로그 서비스 상태를 진단해주세요", "log-service", "disk-full"),
            ]:
                started = time.perf_counter()
                response = client.post("/chat", json={"message": query, "thread_id": thread})
                response.raise_for_status()
                result = response.json()
                item = result["metadata"]["recovery_results"][0]
                assert result["metadata"]["validation_status"] == "validated", result
                assert item["snapshot"]["system_name"] == target
                fetched = {e["target"] for e in result["events"] if e["type"] == "TaskCompleted" and e.get("source") == "runbook"}
                assert runbook in fetched
                assert f"CPU {item['snapshot']['cpu_percent']:g}%" in result["reply"]
                follow = client.post("/chat", json={"message": "그 서비스 다시 확인해주세요", "thread_id": thread})
                follow.raise_for_status()
                follow = follow.json()
                next_item = follow["metadata"]["recovery_results"][0]
                assert next_item["snapshot"]["system_name"] == target
                assert next_item["snapshot"]["sample_id"] != item["snapshot"]["sample_id"]
                records.append({"query": query, "target": target, "required_runbook": runbook,
                                "diagnosis": result, "followup": follow,
                                "elapsed_including_followup_seconds": time.perf_counter() - started})
                save()
                print(json.dumps({"target": target, "diagnosis": "validated", "followup_target": "matched"}), flush=True)
            report = client.post("/chat", json={"message": "보고서 내보내기", "thread_id": thread}).json()
            assert report["metadata"]["reports"][0]["system_name"] == "log-service"
        status = "passed"
        save()
    except Exception as exc:
        status = "failed"
        save(f"{type(exc).__name__}: {str(exc)[:300]}")
        raise


if __name__ == "__main__":
    main()
