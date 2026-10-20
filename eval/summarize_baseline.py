"""Summarize measured baseline records without inventing missing results."""
import json
from collections import defaultdict
from pathlib import Path
from statistics import median


root = Path("results/baseline-2026-10-20")
groups = defaultdict(list)
for path in sorted((root / "ollama").glob("round-*.json")):
    row = json.loads(path.read_text())
    groups[row["case_id"]].append(row)
assert sum(map(len, groups.values())) == 18, "Wait for all 18 planned records"
assert all(len(rows) == 3 and all(r["completed"] for r in rows) for rows in groups.values())
summary = {}
for case, rows in groups.items():
    times = [r["elapsed_s"] for r in rows]
    summary[case] = {
        "requests": len(rows), "completed": sum(r["completed"] for r in rows),
        "expected_route_matches": sum(r["checks"]["expected_route"] for r in rows),
        "expected_target_matches": sum(r["checks"]["expected_target_set"] for r in rows),
        "observed_targets": [r["observed"]["targets"] for r in rows],
        "observed_runbooks": [r["observed"]["runbooks"] for r in rows],
        "median_s": round(median(times), 3), "min_s": min(times), "max_s": max(times),
    }
(root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
labels = {"greeting": "인사", "explicit": "결제 서비스 명시", "missing": "서비스명 누락",
          "unknown": "미등록 billing-service", "multi": "결제+캐시 복수 서비스", "network": "게이트웨이 타임아웃"}
lines = ["| 시나리오 | 완료 | 기대 대상 처리 일치 | 실제 조회 대상(회차순) | 중앙값(초) | 범위(초) |",
         "|---|---:|---:|---|---:|---|" ]
for case in labels:
    s = summary[case]
    targets = " / ".join(", ".join(t) or "조회 없음" for t in s["observed_targets"])
    lines.append(f"| {labels[case]} | {s['completed']}/3 | {s['expected_target_matches']}/3 | {targets} | {s['median_s']} | {s['min_s']}-{s['max_s']} |")
(root / "summary-table.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
