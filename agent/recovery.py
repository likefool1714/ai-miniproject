"""순수 상태 비교/복구 판정. 모델의 문장 대신 관측값과 공개된 정책을 사용한다."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

UTC = timezone.utc
PHASE_LABELS = {
    "recovered": "복구 확인", "partial": "부분 회복", "ongoing": "장애 지속",
    "hold": "판단 보류",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    # 제공 스켈레톤의 last_checked는 로컬 시각이며 timezone 정보가 없다.
    return value.astimezone(UTC)


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    cpu_limit: float = Field(default=80, gt=0, le=100)
    memory_limit: float = Field(default=85, gt=0, le=100)
    freshness_seconds: int = Field(default=300, gt=0)
    required_normal_samples: int = Field(default=2, ge=2, le=10)
    minimum_interval_seconds: int = Field(default=5, ge=1)
    recurrence_window_seconds: int = Field(default=3600, gt=0)
    future_tolerance_seconds: int = Field(default=5, ge=0)

    @classmethod
    def load(cls, path: Path | None = None) -> "RecoveryPolicy":
        path = path or Path(__file__).resolve().parent.parent / "data/recovery_policy.json"
        return cls.model_validate_json(path.read_text())


class Observation(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    system_name: str = Field(min_length=1, max_length=100)
    status: Literal["healthy", "degraded", "down", "unknown"]
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    last_checked: datetime
    alerts: list[str]
    sample_id: str = ""
    data_source: str = "sample-monitoring"

    @field_validator("last_checked")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return aware(value)

    @classmethod
    def from_payload(cls, payload: dict) -> "Observation":
        observation = cls.model_validate(payload)
        if not observation.sample_id:
            raw = observation.model_dump_json()
            observation.sample_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
        return observation


def issues(sample: Observation, policy: RecoveryPolicy) -> list[str]:
    found = []
    if sample.status != "healthy":
        found.append(f"서비스 상태 {sample.status}")
    if sample.cpu_percent >= policy.cpu_limit:
        found.append(f"CPU {sample.cpu_percent:g}% (정상 기준 < {policy.cpu_limit:g}%)")
    if sample.memory_percent >= policy.memory_limit:
        found.append(f"메모리 {sample.memory_percent:g}% (정상 기준 < {policy.memory_limit:g}%)")
    if sample.alerts:
        found.append(f"활성 알림 {len(sample.alerts)}건")
    return found


def freshness_problem(sample: Observation, now: datetime, policy: RecoveryPolicy) -> str | None:
    age = (aware(now) - sample.last_checked).total_seconds()
    if age < -policy.future_tolerance_seconds:
        return "관측 시각이 미래여서 신뢰할 수 없습니다."
    if age > policy.freshness_seconds:
        return f"관측 자료가 {policy.freshness_seconds}초 유효시간을 초과했습니다."
    if sample.status == "unknown":
        return "서비스 상태를 확인하지 못했습니다. 기본값을 측정값으로 사용하지 않습니다."
    return None


def assess(rows: list[dict], policy: RecoveryPolicy, now: datetime,
           continuity_after_id: int | None = None) -> dict:
    """rows는 오래된 순서의 {snapshot, assessment?}. 같은 관측은 한 번만 센다.

    정상 2회는 실제 복구 증명이 아니라 이 데모의 운영 정책이다. 정상 관측 사이에
    최소 간격이 필요하며, 관측되지 않은 시간 동안의 정상 상태를 보장하지 않는다.
    """
    result = {"phase": "hold", "recurrence": False, "normal_streak": 0,
              "comparison": None, "reasons": [], "policy": policy.model_dump()}
    if not rows:
        result["reasons"] = ["저장된 관측이 없습니다."]
        return result
    samples = [Observation.from_payload(r["snapshot"]) for r in rows]
    current = samples[-1]
    result["sample_id"] = current.sample_id
    problem = freshness_problem(current, now, policy)
    if problem:
        result["reasons"] = [problem]
        return result
    if rows[-1].get("assessment", {}).get("observation_usable") is False:
        result["reasons"] = ["최근 관측의 검증에 실패했습니다. 새 관측이 필요합니다."]
        return result
    last_usable = next((s for r, s in reversed(list(zip(rows[:-1], samples[:-1])))
                        if r.get("assessment", {}).get("observation_usable", True)
                        and not freshness_problem(s, now, policy)), None)
    if last_usable and current.last_checked <= last_usable.last_checked:
        result["reasons"] = ["관측 시각이 이전 기록보다 늦지 않아 순서대로 비교할 수 없습니다."]
        return result

    current_issues = issues(current, policy)
    result["issues"] = current_issues
    previous = samples[-2] if len(samples) > 1 else None
    if previous and rows[-2].get("assessment", {}).get("observation_usable", True) and not freshness_problem(previous, now, policy):
        cpu = round(current.cpu_percent - previous.cpu_percent, 2)
        memory = round(current.memory_percent - previous.memory_percent, 2)
        added = sorted(set(current.alerts) - set(previous.alerts))
        cleared = sorted(set(previous.alerts) - set(current.alerts))
        changes = [cpu, memory, len(current.alerts) - len(previous.alerts),
                   {"healthy": 0, "degraded": 1, "down": 2, "unknown": 3}[current.status]
                   - {"healthy": 0, "degraded": 1, "down": 2, "unknown": 3}[previous.status]]
        improving = all(x <= 0 for x in changes) and any(x < 0 for x in changes) and not added
        worsening = all(x >= 0 for x in changes) and (any(x > 0 for x in changes) or bool(added))
        trend = "개선" if improving else "악화" if worsening else "변화 없음" if not any(changes) and not added and not cleared else "혼재"
        result["comparison"] = {"cpu_delta_pp": cpu, "memory_delta_pp": memory,
                                "alerts_added": added, "alerts_cleared": cleared,
                                "previous_checked": previous.last_checked.isoformat(),
                                "trend": trend}

    # 현재부터 역순으로, 신선하고 시간 순서가 맞는 동일 서비스 관측만 비교한다.
    recent = []
    seen_ids = set()
    last_time = None
    for row, sample in reversed(list(zip(rows, samples))):
        if sample.system_name != current.system_name or freshness_problem(sample, now, policy):
            break
        if row.get("assessment", {}).get("observation_usable") is False:
            break
        if last_time is not None and sample.last_checked >= last_time:
            break
        if sample.sample_id in seen_ids:
            continue
        recent.append((sample, row.get("id")))
        seen_ids.add(sample.sample_id)
        last_time = sample.last_checked

    normal_streak = 0
    last_counted = None
    for sample, row_id in recent:
        if continuity_after_id is not None and row_id is not None and row_id <= continuity_after_id:
            break
        if issues(sample, policy):
            break
        if last_counted is None or (last_counted - sample.last_checked).total_seconds() >= policy.minimum_interval_seconds:
            normal_streak += 1
            last_counted = sample.last_checked
    result["normal_streak"] = normal_streak
    # 자료 공백 이전의 유효한 장애 이력은 보존하되 정상 횟수는 공백 뒤부터 다시 센다.
    had_incident = any(
        s.system_name == current.system_name and s.last_checked < current.last_checked
        and not freshness_problem(s, now, policy)
        and r.get("assessment", {}).get("observation_usable", True)
        and issues(s, policy)
        for r, s in zip(rows[:-1], samples[:-1]))
    if not current_issues:
        if not had_incident:
            result["reasons"] = ["현재 정상 기준은 충족하지만 최근 장애 기록이 없어 복구 여부를 확정하지 않습니다."]
        elif normal_streak >= policy.required_normal_samples:
            result["phase"] = "recovered"
            result["reasons"] = [f"최근 장애 이후 {policy.minimum_interval_seconds}초 이상 간격의 정상 관측 {normal_streak}회가 확인됐습니다."]
        else:
            result["phase"] = "partial"
            result["reasons"] = [f"현재 정상 기준은 충족했지만 정상 관측 {normal_streak}/{policy.required_normal_samples}회로 복구 확정을 보류합니다."]
    else:
        result["phase"] = "partial" if result["comparison"] and result["comparison"]["trend"] == "개선" else "ongoing"
        result["reasons"] = current_issues
        result["recurrence"] = any(
            r.get("assessment", {}).get("phase") == "recovered"
            and r.get("assessment", {}).get("policy") == policy.model_dump()
            and 0 <= (current.last_checked - s.last_checked).total_seconds() <= policy.recurrence_window_seconds
            for r, s in zip(rows[:-1], samples[:-1])
        )
    if len(samples) == 1:
        result["reasons"].append("이전 조사 기록이 없어 변화량을 비교할 수 없습니다.")
    elif result["comparison"] is None:
        result["reasons"].append("이전 자료가 오래됐거나 미확인 상태여서 변화량 비교를 보류합니다.")
    if continuity_after_id is not None:
        result["reasons"].append("마지막 조회 실패 이전의 정상 관측은 연속 횟수에 포함하지 않습니다.")
    return result


def explain_assessment(snapshot: dict, assessment: dict) -> str:
    """복구 판정의 의미와 다음 확인을 설명한다. 조회 실패·오래된 수치는 해석하지 않는다."""
    phase = assessment["phase"]
    if phase == "hold":
        return "지금 확보한 자료만으로 현재 복구 상태를 판단하기 어렵습니다. " + " ".join(assessment["reasons"]) + " 새 관측을 확보한 뒤 다시 판단해야 합니다."
    remaining = assessment.get("issues", [])
    policy = assessment["policy"]
    comparison = assessment.get("comparison")
    if phase == "recovered":
        return ("현재 서비스 상태·CPU·메모리·알림이 모두 설정한 정상 기준을 충족하고, "
                "장애 이후 간격을 둔 정상 관측도 필요한 횟수만큼 확인되어 정책상 복구로 판단합니다. "
                "복구 이후에도 새 관측에서 수치와 알림이 다시 악화되는지 확인하세요. 실제 사용자 요청 성공까지 검증한 것은 아닙니다.")
    if phase == "partial" and not remaining:
        return ("현재 수치와 알림은 정상 기준으로 돌아왔습니다. 하지만 일시적으로만 정상화됐을 가능성을 배제하려면 추가 관측이 필요합니다. "
                f"현재 정상 관측은 {assessment['normal_streak']}/{policy['required_normal_samples']}회이므로 복구 확정은 보류합니다. "
                f"관측 시각이 최소 {policy['minimum_interval_seconds']}초 이상 떨어진 새 자료에서 정상 상태가 이어지는지 다시 확인하세요.")
    opening = ("이전보다 지표가 개선됐지만 아직 정상 기준을 모두 충족하지 못했습니다." if phase == "partial" else
               "현재 정상 기준을 충족하지 못한 항목이 있어 장애가 지속되는 상태로 판단합니다.")
    if comparison and comparison["trend"] in ("악화", "혼재"):
        opening += " 이전 대비 지표가 악화됐습니다." if comparison["trend"] == "악화" else " 지표별 변화 방향이 달라 일부 수치가 좋아진 것만으로 복구를 판단할 수 없습니다."
    if assessment.get("recurrence"):
        opening = "최근 복구 확인 이후 이상이 다시 관측되어 장애 재발로 표시합니다. 같은 원인인지는 추가 조사가 필요합니다. " + opening
    checks = []
    if snapshot["status"] != "healthy":
        checks.append("서비스 응답 상태")
    if snapshot["cpu_percent"] >= policy["cpu_limit"]:
        checks.append("CPU 부하")
    if snapshot["memory_percent"] >= policy["memory_limit"]:
        checks.append("메모리 사용 추세")
    if snapshot["alerts"]:
        checks.append("남아 있는 알림의 원인과 해소 여부")
    return opening + " " + "·".join(checks) + "를 우선 점검하고, 조치 후 새 관측으로 재확인하세요. 복구 여부 판정만으로 근본 원인이 밝혀진 것은 아닙니다."


def render_assessment(system_name: str, snapshot: dict, assessment: dict) -> str:
    label = PHASE_LABELS[assessment["phase"]]
    if assessment.get("recurrence"):
        label = "장애 재발 / " + label
    lines = [f"### {system_name}: {label}", explain_assessment(snapshot, assessment), ""]
    if assessment.get("issues") is not None:
        lines.append(f"- 관측: 상태={snapshot['status']}, CPU {snapshot['cpu_percent']:g}%, 메모리 {snapshot['memory_percent']:g}%, 알림 {len(snapshot['alerts'])}건")
        lines.append(f"- 활성 알림: {', '.join(snapshot['alerts']) or '없음'}")
    lines.append(f"- 관측 시각: {snapshot['last_checked']}")
    comparison = assessment.get("comparison")
    if comparison:
        lines.append(f"- 이전 대비: CPU {comparison['cpu_delta_pp']:+g}%p, 메모리 {comparison['memory_delta_pp']:+g}%p ({comparison['trend']})")
        lines.append(f"- 새 알림: {', '.join(comparison['alerts_added']) or '없음'}")
        lines.append(f"- 해소된 알림: {', '.join(comparison['alerts_cleared']) or '없음'}")
    lines += [f"- 판단 근거: {reason}" for reason in assessment["reasons"]]
    lines.append(f"- 출처: {snapshot.get('data_source', 'sample-monitoring')} / 관측 {snapshot.get('sample_id', '미지정')}")
    return "\n".join(lines)
