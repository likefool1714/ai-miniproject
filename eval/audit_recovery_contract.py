"""고정 데모 외 입력으로 복구 계약을 확인한다. 모델 정답률 평가가 아니다."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path

from agent.recovery import RecoveryPolicy, assess
from agent.recovery_store import RecoveryStore


def audit():
    now = [datetime(2026, 10, 20, 8, tzinfo=timezone.utc)]
    def payload(identifier, bad=False, **changes):
        return {"system_name": "payments-service", "status": "degraded" if bad else "healthy",
                "cpu_percent": 95 if bad else 50, "memory_percent": 50, "alerts": [],
                "last_checked": now[0].isoformat(), "sample_id": str(identifier), **changes}
    def record(store, identifier, bad=False):
        now[0] += timedelta(seconds=10)
        p = payload(identifier, bad)
        return p, store.record("audit", p)
    edges = {}
    with_store = lambda: RecoveryStore(":memory:", clock=lambda: now[0])
    store = with_store()
    record(store, 0, True); record(store, 1)
    store.failed("audit", "payments-service", "connection failed")
    _, result = record(store, 2)
    edges["failure_between_normal_samples"] = {"phase": result["assessment"]["phase"], "normal_streak": result["assessment"]["normal_streak"]}
    assert result["assessment"]["phase"] == "partial" and result["assessment"]["normal_streak"] == 1
    assert record(store, 3)[1]["assessment"]["phase"] == "recovered"
    store.close()
    store = with_store()
    record(store, 0, True); record(store, 1); p, _ = record(store, 2)
    store.failed("audit", "payments-service", "connection failed")
    result = store.record("audit", p)
    edges["failed_recheck_then_duplicate_old_normal"] = {"phase": result["assessment"]["phase"], "failure_preserved": "최근 재점검 실패" in store.report("audit", "payments-service")}
    assert result["assessment"]["phase"] == "hold" and edges["failed_recheck_then_duplicate_old_normal"]["failure_preserved"]
    store.close()
    store = with_store()
    record(store, 0, True)
    for i in range(1, 131):
        now[0] += timedelta(seconds=1)
        result = store.record("audit", payload(i))
    heading = next(line for line in store.report("audit", "payments-service").splitlines() if line.startswith("### "))
    edges["more_than_100_fresh_observations"] = {"phase": result["assessment"]["phase"], "report_heading": heading}
    assert result["assessment"]["phase"] == "recovered" and "복구 확인" in heading
    store.close()
    combinations = []
    for status, cpu, memory, alarm in product(
            ["healthy", "degraded", "down", "unknown"],
            [0, 79.9, 80, 80.1, 89.5, 90, 100],
            [0, 84.9, 85, 85.1, 89.9, 90, 100], [False, True]):
        # 오라클은 공개한 정상 조건의 직접 표현이다. 구현의 phase 분기를 복사하지 않는다.
        expected = status == "healthy" and cpu < 80 and memory < 85 and not alarm
        rows = [{"snapshot": payload("bad", True, last_checked=(now[0] - timedelta(seconds=20)).isoformat())},
                {"snapshot": payload("normal", last_checked=(now[0] - timedelta(seconds=10)).isoformat())},
                {"snapshot": payload("current", status=status, cpu_percent=cpu, memory_percent=memory, alerts=["alarm"] if alarm else [])}]
        result = assess(rows, RecoveryPolicy(), now[0])
        matched = (result["phase"] == "recovered") == expected
        combinations.append({"status": status, "cpu": cpu, "memory": memory, "alarm": alarm,
                             "expected_recovered": expected, "phase": result["phase"], "matched": matched})
    assert all(c["matched"] for c in combinations)
    return {"status": "passed", "scope": "synthetic fixed-clock contract checks; no real incident/root-cause accuracy claim",
            "edge_cases": edges, "threshold_combinations": len(combinations),
            "matched": sum(c["matched"] for c in combinations), "cases": combinations}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="results/professor-audit-2026-10-20")
    args = parser.parse_args()
    output = Path(__file__).resolve().parent.parent / args.output
    output.mkdir(parents=True, exist_ok=True)
    result = audit()
    (output / "contract-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False, indent=2))
