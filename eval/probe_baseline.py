"""Read-only baseline probes: exercise the unmodified HTTP/SSE application.

Run with the agent/frontend already running. Each round uses fresh thread IDs.
Expected behavior describes proposed acceptance criteria, not existing promises.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


CASES = [
    ("greeting", "안녕하세요", "chat", [], None),
    ("explicit", "payments-service 결제 응답이 느려지고 CPU가 치솟았어요, 원인 확인해주세요", "incident", ["payments-service"], "cpu-spike"),
    ("missing", "로그인 응답이 느려요, 원인 진단해주세요", "incident", [], None),
    ("unknown", "billing-service 응답이 느려요. 원인 진단해주세요", "incident", [], None),
    ("multi", "payments-service랑 cache-service 둘 다 저하됐는데 공통 원인이 있는지 분석해줘", "incident", ["payments-service", "cache-service"], None),
    ("network", "gateway-service 요청이 타임아웃 나고 있어요. 원인 진단해주세요", "incident", ["gateway-service"], "network-timeout"),
]


def probe(base_url, case, round_number, run_id, output):
    case_id, message, expected_route, expected_targets, expected_runbook = case
    record = {
        "case_id": case_id, "round": round_number, "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "expected": {"route": expected_route, "targets": expected_targets,
                     "runbook": expected_runbook},
        "thread_id": f"baseline-{run_id}-{round_number}-{case_id}",
        "events": [], "completed": False,
    }
    params = urlencode({"message": message, "thread_id": record["thread_id"]})
    start = time.perf_counter()
    first_token = None
    tokens = 0
    try:
        with urlopen(f"{base_url}/chat/stream?{params}", timeout=480) as response:
            record["http_status"] = response.status
            for line in response:
                if not line.startswith(b"data: "):
                    continue
                payload = line[6:].decode("utf-8").strip()
                if payload == "[DONE]":
                    record["completed"] = True
                    break
                event = json.loads(payload)
                elapsed = round(time.perf_counter() - start, 3)
                if event.get("type") == "AnswerToken":
                    tokens += 1
                    if first_token is None:
                        first_token = elapsed
                    continue
                if event.get("type") == "FinalAnswer":
                    record["reply"] = event.get("reply", "")
                    record["final_prompt"] = event.get("prompt")
                else:
                    record["events"].append({**event, "received_after_s": elapsed})
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsed_s"] = round(time.perf_counter() - start, 3)
    record["first_answer_token_s"] = first_token
    record["answer_token_events"] = tokens
    route = next((e.get("route") for e in record["events"]
                  if e.get("type") == "RouteDecided"), None)
    targets = [e["target"] for e in record["events"]
               if e.get("type") == "TaskCompleted" and e.get("source") == "monitoring"]
    runbooks = [e["target"] for e in record["events"]
                if e.get("type") == "TaskCompleted" and e.get("source") == "runbook"]
    record["observed"] = {"route": route, "targets": targets, "runbooks": runbooks}
    record["checks"] = {
        "stream_completed": record["completed"],
        "expected_route": route == expected_route,
        "expected_target_set": set(targets) == set(expected_targets),
        "expected_runbook": None if expected_runbook is None else expected_runbook in runbooks,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / f"round-{round_number}-{case_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"round": round_number, "case": case_id,
                      "elapsed_s": record["elapsed_s"], **record["observed"],
                      "checks": record["checks"], "error": record.get("error")}, ensure_ascii=False), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8600")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("results/baseline-2026-10-20/ollama"))
    args = parser.parse_args()
    if args.output.exists() and any(args.output.glob("round-*.json")):
        parser.error("Output already contains evidence; choose a fresh --output directory")
    run_id = datetime.now(timezone.utc).strftime("%H%M%S")
    args.output.mkdir(parents=True, exist_ok=True)
    with urlopen(args.base_url + "/health", timeout=15) as response:
        health = json.load(response)
    (args.output / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n")
    for round_number in range(1, args.rounds + 1):
        for case in CASES:
            probe(args.base_url, case, round_number, run_id, args.output)


if __name__ == "__main__":
    main()
