"""SQLite 조사 이력과 대화별 대상 상태. 관측 저장과 판정을 한 트랜잭션으로 묶는다."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock

from .recovery import Observation, RecoveryPolicy, assess, freshness_problem, render_assessment, utc_now


class RecoveryStore:
    def __init__(self, db_path: Path | str, policy: RecoveryPolicy | None = None, clock=utc_now):
        self.policy = policy or RecoveryPolicy.load()
        self.clock = clock
        self._lock = RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS recovery_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL, system_name TEXT NOT NULL,
                sample_id TEXT NOT NULL, snapshot TEXT NOT NULL,
                assessment TEXT NOT NULL, recorded_at TEXT NOT NULL,
                UNIQUE(thread_id, system_name, sample_id)
            );
            CREATE INDEX IF NOT EXISTS recovery_history_idx
            ON recovery_observations(thread_id, system_name, id);
            CREATE TABLE IF NOT EXISTS recovery_context (
                thread_id TEXT PRIMARY KEY, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recovery_failures (
                thread_id TEXT, system_name TEXT, reason TEXT, checked_at TEXT,
                PRIMARY KEY(thread_id, system_name)
            );
            CREATE TABLE IF NOT EXISTS recovery_continuity (
                thread_id TEXT, system_name TEXT, after_id INTEGER NOT NULL, reason TEXT,
                PRIMARY KEY(thread_id, system_name)
            );
            INSERT OR IGNORE INTO recovery_continuity
            SELECT f.thread_id, f.system_name, COALESCE(MAX(o.id), 0), f.reason
            FROM recovery_failures f LEFT JOIN recovery_observations o
              ON o.thread_id=f.thread_id AND o.system_name=f.system_name
            GROUP BY f.thread_id, f.system_name;
        """)
        self.conn.commit()

    def context(self, thread_id: str) -> dict:
        with self._lock:
            row = self.conn.execute("SELECT data FROM recovery_context WHERE thread_id=?", (thread_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def set_context(self, thread_id: str, **changes) -> None:
        with self._lock, self.conn:
            context = self.context(thread_id)
            context.update(changes)
            self.conn.execute("INSERT INTO recovery_context VALUES (?,?) ON CONFLICT(thread_id) DO UPDATE SET data=excluded.data",
                              (thread_id, json.dumps(context, ensure_ascii=False)))

    @staticmethod
    def _decode(row) -> dict:
        return {"id": row["id"], "snapshot": json.loads(row["snapshot"]),
                "assessment": json.loads(row["assessment"]), "recorded_at": row["recorded_at"]}

    def history(self, thread_id: str, system_name: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM recovery_observations WHERE thread_id=? AND system_name=? ORDER BY id DESC LIMIT ?",
                (thread_id, system_name, max(1, min(limit, 500))),
            ).fetchall()
            return [self._decode(r) for r in reversed(rows)]

    def record(self, thread_id: str, payload: dict) -> dict:
        sample = Observation.from_payload(payload)
        snapshot = sample.model_dump(mode="json")
        now: datetime = self.clock()
        with self._lock, self.conn:
            rows = self.history(thread_id, sample.system_name, 500)
            duplicate = self.conn.execute(
                "SELECT id,snapshot FROM recovery_observations WHERE thread_id=? AND system_name=? AND sample_id=?",
                (thread_id, sample.system_name, sample.sample_id),
            ).fetchone()
            if duplicate:
                if json.loads(duplicate["snapshot"]) != snapshot:
                    reason = "동일 관측 ID의 내용이 달라 복구 판단을 보류합니다."
                    self._mark_failed(thread_id, sample.system_name, reason)
                    return {"id": duplicate["id"], "snapshot": snapshot,
                            "assessment": {"phase": "hold", "recurrence": False, "normal_streak": 0,
                                           "comparison": None, "reasons": [reason], "policy": self.policy.model_dump()},
                            "duplicate": True, "usable": False}
                # 동일 관측은 횟수를 늘리지 않고 최신 이력의 신선도만 재평가한다.
                if duplicate["id"] != rows[-1]["id"]:
                    self._mark_failed(thread_id, sample.system_name,
                                      "과거 관측 ID가 다시 반환되어 최신 상태를 확인하지 못했습니다.")
                result = self._assess(thread_id, sample.system_name, rows, now)
                result["reasons"].append("동일 관측 재조회는 정상 관측 횟수에 추가하지 않습니다.")
                failure = self.conn.execute("SELECT reason FROM recovery_failures WHERE thread_id=? AND system_name=?",
                                            (thread_id, sample.system_name)).fetchone()
                if failure:
                    result.update(phase="hold", recurrence=False,
                                  reasons=[failure[0], "동일·과거 관측 재조회로 최근 실패를 해소하지 않습니다. 새 관측이 필요합니다."])
                usable = not failure and not freshness_problem(Observation.from_payload(rows[-1]["snapshot"]), now, self.policy)
                return {"id": rows[-1]["id"], "snapshot": rows[-1]["snapshot"], "assessment": result,
                        "duplicate": True, "usable": bool(usable)}
            problem = freshness_problem(sample, now, self.policy)
            last_usable = next((r for r in reversed(rows) if r["assessment"].get("observation_usable", True)
                                and not freshness_problem(Observation.from_payload(r["snapshot"]), now, self.policy)), None)
            if last_usable and sample.last_checked <= Observation.from_payload(last_usable["snapshot"]).last_checked:
                problem = "관측 시각이 이전 기록보다 늦지 않아 최신 상태를 확인하지 못했습니다."
            result = self._assess(thread_id, sample.system_name, (rows + [{"snapshot": snapshot}])[-500:], now)
            result["observation_usable"] = not bool(problem)
            cursor = self.conn.execute(
                "INSERT INTO recovery_observations(thread_id,system_name,sample_id,snapshot,assessment,recorded_at) VALUES (?,?,?,?,?,?)",
                (thread_id, sample.system_name, sample.sample_id,
                 json.dumps(snapshot, ensure_ascii=False), json.dumps(result, ensure_ascii=False), now.isoformat()),
            )
            if problem:
                self._mark_failed(thread_id, sample.system_name, problem)
            else:
                self.conn.execute("DELETE FROM recovery_failures WHERE thread_id=? AND system_name=?", (thread_id, sample.system_name))
            return {"id": cursor.lastrowid, "snapshot": snapshot, "assessment": result,
                    "duplicate": False, "usable": not bool(problem)}

    def _assess(self, thread_id, system_name, rows, now):
        boundary = self.conn.execute("SELECT after_id FROM recovery_continuity WHERE thread_id=? AND system_name=?",
                                     (thread_id, system_name)).fetchone()
        return assess(rows, self.policy, now, continuity_after_id=boundary[0] if boundary else None)

    def _mark_failed(self, thread_id, system_name, reason):
        # 호출자는 잠금과 트랜잭션을 소유한다. 실패 시점의 마지막 관측 ID를 경계로 보존한다.
        self.conn.execute("INSERT INTO recovery_failures VALUES (?,?,?,?) ON CONFLICT(thread_id,system_name) DO UPDATE SET reason=excluded.reason, checked_at=excluded.checked_at",
                          (thread_id, system_name, reason, self.clock().isoformat()))
        boundary = self.conn.execute("SELECT COALESCE(MAX(id),0) FROM recovery_observations WHERE thread_id=? AND system_name=?",
                                     (thread_id, system_name)).fetchone()[0]
        self.conn.execute("INSERT INTO recovery_continuity VALUES (?,?,?,?) ON CONFLICT(thread_id,system_name) DO UPDATE SET after_id=excluded.after_id, reason=excluded.reason",
                          (thread_id, system_name, boundary, reason))

    def failed(self, thread_id: str, system_name: str, reason: str):
        with self._lock, self.conn:
            self._mark_failed(thread_id, system_name, reason)

    def report(self, thread_id: str, system_name: str) -> str | None:
        with self._lock:
            full_history = self.history(thread_id, system_name, 500)
            if not full_history:
                return None
            rows = full_history[-100:]
            latest = rows[-1]
            current_assessment = self._assess(thread_id, system_name, full_history, self.clock())
            failure = self.conn.execute("SELECT reason,checked_at FROM recovery_failures WHERE thread_id=? AND system_name=?", (thread_id, system_name)).fetchone()
        if failure:
            current_assessment.update(phase="hold", recurrence=False, reasons=[f"최근 재점검 실패 ({failure['checked_at']}): {failure['reason']}", "아래 관측은 마지막 성공 조회의 참고 자료입니다."])
        policy = self.policy
        lines = [f"# {system_name} 장애 조사·복구 보고서", "",
                 "샘플 모니터링 기반 데모입니다. 실제 서비스 복구나 원인 확정을 증명하지 않습니다.", "",
                 f"- 생성 시각: {self.clock().isoformat()}", f"- 대화 ID: {thread_id}",
                 f"- 최근 기록: 최대 100건 (현재 {len(rows)}건)", "",
                 render_assessment(system_name, latest["snapshot"], current_assessment), "",
                 "## 조사 이력", "", "| 관측 시각 | 상태 | CPU (%) | 메모리 (%) | 알림 수 | 당시 판정 |",
                 "|---|---|---:|---:|---:|---|"]
        from .recovery import PHASE_LABELS
        for row in rows:
            s, a = row["snapshot"], row["assessment"]
            phase = PHASE_LABELS[a["phase"]] + (" / 재발" if a.get("recurrence") else "")
            lines.append(f"| {s['last_checked']} | {s['status']} | {s['cpu_percent']:g} | {s['memory_percent']:g} | {len(s['alerts'])} | {phase} |")
        lines += ["", "## 판정 정책과 한계", "",
                  f"- 정상 조건: status=healthy, CPU < {policy.cpu_limit:g}%, 메모리 < {policy.memory_limit:g}%, 활성 알림 0건.",
                  f"- 최근 장애 기록 이후 {policy.minimum_interval_seconds}초 이상 간격으로 정상 {policy.required_normal_samples}회 필요.",
                  "- 조회 실패가 끼면 이후의 신규 관측부터 정상 횟수를 다시 셉니다. 동일·과거 관측은 최근 실패를 해소하지 않습니다.",
                  "- 현재 판정은 최근 최대 500건, 아래 이력 표는 최근 최대 100건을 사용합니다.",
                  f"- 비교 자료 유효시간 {policy.freshness_seconds}초, 재발 연관 기간 {policy.recurrence_window_seconds}초.",
                  f"- 임계값과 {policy.required_normal_samples}회 정상 기준은 교육용 정책이며 실제 SLO나 운영 복구 기준이 아닙니다.",
                  "- 보고서 열람은 새 모니터링 조회를 수행하지 않습니다. 오래된 자료는 판단 보류합니다.",
                  "- 관측 사이의 상태, 실제 사용자 트랜잭션, 로그 및 장애 원인은 검증하지 않습니다.",
                  "- 조사 이력은 대화·서비스별로 분리합니다. 서비스 재시작·롤백을 실행하지 않습니다."]
        return "\n".join(lines) + "\n"

    def close(self):
        self.conn.close()
