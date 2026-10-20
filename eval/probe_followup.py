"""Reuse the three explicit-case threads to check incident follow-up context."""
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen


root = Path("results/baseline-2026-10-20/ollama")
if any(root.glob("followup-*.json")):
    raise SystemExit("Supplementary evidence already exists; preserve it before rerunning")
for round_number in range(1, 4):
    seed = json.loads((root / f"round-{round_number}-explicit.json").read_text())
    body = {"message": "그 서비스의 메모리도 진단해줘", "thread_id": seed["thread_id"]}
    request = Request("http://127.0.0.1:8100/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    record = {"round": round_number, "seed_file": f"round-{round_number}-explicit.json",
              "request": body, "expected_targets": ["payments-service"]}
    try:
        with urlopen(request, timeout=120) as response:
            record["http_status"] = response.status
            record["response"] = json.load(response)
        record["observed_targets"] = [e["target"] for e in record["response"]["events"]
                                      if e.get("type") == "TaskCompleted" and e.get("source") == "monitoring"]
        record["target_matches_previous_request"] = set(record["observed_targets"]) == {"payments-service"}
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsed_s"] = round(time.perf_counter() - start, 3)
    (root / f"followup-{round_number}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k != "response"}, ensure_ascii=False), flush=True)
