"""수집 근거 → 구조화된 모델 제안 → 코드 검증 → 답변 렌더링.

모델이 생성한 자유 문장과 수치를 최종 답변에 직접 사용하지 않는다.
강의의 Pydantic 스키마, 권위 게이트, Validator를 적용한 제한된 작성 방식이다.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .recovery import Observation, RecoveryPolicy, freshness_problem, utc_now

Kind = Literal["cpu", "memory", "latency", "network", "disk", "service_down", "unknown"]
RUNBOOK_KIND = {"cpu-spike": "cpu", "memory-leak": "memory", "high-latency": "latency",
                "network-timeout": "network", "disk-full": "disk", "db-down": "service_down"}
CAUSE_TEXT = {
    "cpu": "CPU 부하 원인을 추가 확인할 필요가 있습니다. 원인은 확정되지 않았습니다.",
    "memory": "메모리 압박이 의심됩니다. 누수 여부는 추세·힙 자료로 추가 확인해야 합니다.",
    "latency": "지연 또는 커넥션 풀 관련 알림이 있습니다. 병목 원인은 미확정입니다.",
    "network": "타임아웃 또는 네트워크 관련 알림이 있습니다. 장애 구간은 추가 확인이 필요합니다.",
    "disk": "디스크 관련 알림이 있습니다. 용량과 정리 대상은 추가 확인이 필요합니다.",
    "service_down": "서비스 중단 상태가 관측되었습니다. 의존성 장애 여부는 미확정입니다.",
    "unknown": "이 관측만으로 장애 원인을 특정할 수 없습니다.",
}


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_id: str = Field(pattern=r"^M[1-9][0-9]*$")
    kind: Kind


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_id: str = Field(pattern=r"^M[1-9][0-9]*$")
    step_id: str = Field(pattern=r"^R[1-9][0-9]*\.[1-9][0-9]*$")


class WriterDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hypotheses: list[Hypothesis] = Field(max_length=36)
    recommendations: list[Recommendation] = Field(max_length=36)


def supported_kinds(sample: Observation, policy: RecoveryPolicy) -> list[str]:
    alerts = " ".join(sample.alerts).lower()
    kinds = []
    if sample.cpu_percent >= policy.cpu_limit or "cpu" in alerts:
        kinds.append("cpu")
    if sample.memory_percent >= policy.memory_limit or re.search(r"memory|heap|메모리", alerts):
        kinds.append("memory")
    if re.search(r"latency|pool|지연", alerts):
        kinds.append("latency")
    if re.search(r"timeout|network|packet loss|네트워크", alerts):
        kinds.append("network")
    if re.search(r"disk|디스크", alerts):
        kinds.append("disk")
    if sample.status == "down":
        kinds.append("service_down")
    return kinds or ["unknown"]


def incident_types(sample: Observation, policy: RecoveryPolicy) -> list[str]:
    kinds = supported_kinds(sample, policy)
    types = [t for t, kind in RUNBOOK_KIND.items() if kind in kinds and t != "db-down"]
    # 서비스 down만으로 DB 장애를 단정하지 않는다.
    if sample.status == "down" and re.search(r"\bdb\b|database|데이터베이스", " ".join(sample.alerts), re.I):
        types.append("db-down")
    return types


def build_bundle(collected: list[dict], targets: list[str], policy=None, now=None) -> dict:
    policy, now = policy or RecoveryPolicy.load(), now or utc_now()
    latest = {c["data"].get("system_name"): c["data"] for c in collected
              if c.get("source") == "monitoring" and isinstance(c.get("data"), dict)}
    monitoring, unavailable = [], []
    for index, system in enumerate(targets, 1):
        try:
            sample = Observation.from_payload(latest[system])
            problem = freshness_problem(sample, now, policy)
            if sample.status == "unknown":
                problem = "서비스 상태를 알 수 없습니다."
            if problem:
                unavailable.append({"system_name": system, "reason": problem})
                continue
        except (KeyError, ValueError, TypeError):
            unavailable.append({"system_name": system, "reason": "유효한 모니터링 자료가 없습니다."})
            continue
        monitoring.append({"id": f"M{index}", "snapshot": sample.model_dump(mode="json"),
                           "allowed_kinds": supported_kinds(sample, policy),
                           "incident_types": incident_types(sample, policy)})
    runbooks, seen = [], set()
    for c in collected:
        target = c.get("target")
        if c.get("source") != "runbook" or target not in RUNBOOK_KIND or target in seen:
            continue
        if not isinstance(c.get("data"), str):
            continue
        seen.add(target)
        rid = f"R{len(runbooks) + 1}"
        steps = [{"id": f"{rid}.{m.group(1)}", "text": m.group(2)}
                 for line in c["data"].splitlines()
                 if (m := re.match(r"^\s*(\d+)\.\s+(.+)$", line))]
        runbooks.append({"id": rid, "incident_type": target, "steps": steps, "uri": c.get("uri")})
    for m in monitoring:
        m["allowed_step_ids"] = [s["id"] for r in runbooks if r["incident_type"] in m["incident_types"]
                                 for s in r["steps"]]
    # source=analysis는 모델 생성물이며 일차 근거로 전달하지 않는다.
    return {"monitoring": monitoring, "runbooks": runbooks, "unavailable": unavailable}


def validate_draft(raw: str, bundle: dict) -> tuple[WriterDraft | None, list[str]]:
    try:
        draft = WriterDraft.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError):
        return None, ["schema_invalid"]
    sources = {m["id"]: m for m in bundle["monitoring"]}
    errors, covered, seen = [], set(), set()
    for h in draft.hypotheses:
        source = sources.get(h.evidence_id)
        key = (h.evidence_id, h.kind)
        if not source or h.kind not in source["allowed_kinds"]:
            errors.append("unsupported_hypothesis")
        if key in seen:
            errors.append("duplicate_hypothesis")
        seen.add(key)
        covered.add(h.evidence_id)
    if covered != set(sources):
        errors.append("missing_service_hypothesis")
    advised, seen = set(), set()
    for a in draft.recommendations:
        source = sources.get(a.evidence_id)
        key = (a.evidence_id, a.step_id)
        if not source or a.step_id not in source["allowed_step_ids"]:
            errors.append("unsupported_recommendation")
        if key in seen:
            errors.append("duplicate_recommendation")
        seen.add(key)
        advised.add(a.evidence_id)
    if any(m["allowed_step_ids"] and m["id"] not in advised for m in sources.values()):
        errors.append("missing_recommendation")
    if not sources:
        errors.append("no_valid_observation")
    return (None, sorted(set(errors))) if errors else (draft, [])


def fallback_draft(bundle: dict) -> WriterDraft:
    # 검증 실패 시 원인 가설을 채택하지 않고 관측과 조회된 절차만 반환한다.
    return WriterDraft(hypotheses=[], recommendations=[
        Recommendation(evidence_id=m["id"], step_id=m["allowed_step_ids"][0])
        for m in bundle["monitoring"] if m["allowed_step_ids"]])


def default_draft(bundle: dict) -> WriterDraft:
    """Fake/테스트 전용 선택기. 실제 백엔드는 모델이 항목을 선택한다."""
    return WriterDraft(hypotheses=[Hypothesis(evidence_id=m["id"], kind=m["allowed_kinds"][0])
                                  for m in bundle["monitoring"]],
                       recommendations=fallback_draft(bundle).recommendations)


def writer_schema(bundle: dict) -> dict:
    """입력마다 실제 존재하는 ID·분류로 생성 범위를 좁힌다. 서비스별 연결은 별도 검증한다."""
    schema = WriterDraft.model_json_schema()
    if not bundle["monitoring"]:
        return schema
    sources = sorted({m["id"] for m in bundle["monitoring"]})
    kinds = sorted({kind for m in bundle["monitoring"] for kind in m["allowed_kinds"]})
    steps = sorted({step for m in bundle["monitoring"] for step in m["allowed_step_ids"]})
    hypotheses = schema["$defs"]["Hypothesis"]["properties"]
    recommendations = schema["$defs"]["Recommendation"]["properties"]
    hypotheses["kind"]["enum"] = kinds
    for field in (hypotheses["evidence_id"], recommendations["evidence_id"]):
        field.pop("pattern", None)
        field["enum"] = sources
    if steps:
        recommendations["step_id"].pop("pattern", None)
        recommendations["step_id"]["enum"] = steps
    return schema


def writer_prompt(query: str, bundle: dict, feedback=None) -> tuple[str, str]:
    system = (
        "당신은 운영 진단 제안 선택기입니다. JSON 객체만 반환하세요. 자유 문장과 수치는 쓰지 마세요. "
        "각 monitoring의 id마다 hypotheses에 evidence_id와 allowed_kinds 중 kind를 하나 선택하세요. "
        "allowed_step_ids가 있으면 recommendations에 evidence_id와 해당 step_id를 하나 이상 선택하세요. "
        "서비스별 허용 목록을 지키세요. 이 선택은 원인 확정이나 조치 실행을 뜻하지 않습니다. "
        "스키마: " + json.dumps(writer_schema(bundle), ensure_ascii=False))
    user = json.dumps({"query": query, "evidence": bundle, "validation_feedback": feedback}, ensure_ascii=False)
    return system, user


def render_answer(bundle: dict, draft: WriterDraft, status: str) -> str:
    lines = ["## 확인된 모니터링 근거"]
    if status == "fallback":
        lines.append("검증 보류: 모델 제안을 채택하지 않고 확인된 근거와 참고 절차만 제공합니다.")
    elif status == "partial":
        lines.append("일부 자료가 부족합니다. 확보된 근거만 검증했으며 누락된 대상·절차는 판단을 보류합니다.")
    for m in bundle["monitoring"]:
        s = m["snapshot"]
        lines += [f"- [{m['id']}] {s['system_name']}: 상태 {s['status']}, CPU {s['cpu_percent']:g}%, "
                  f"메모리 {s['memory_percent']:g}%, 알림 {len(s['alerts'])}건.",
                  f"  관측 시각 {s['last_checked']} / 관측 ID {s['sample_id']} / 자료 {s['data_source']}"]
        if s["alerts"]:
            lines.append("  모니터링 알림 원문: " + "; ".join(s["alerts"]))
    for missing in bundle["unavailable"]:
        lines.append(f"- {missing['system_name']}: 판단 보류 — {missing['reason']}")
    if draft.hypotheses:
        lines.append("\n## 추가 확인할 원인 후보 (미확정)")
        for h in draft.hypotheses:
            lines.append(f"- [{h.evidence_id}] {CAUSE_TEXT[h.kind]}")
    lines.append("\n## 권장 확인·대응 절차 (미실행)")
    for r in bundle["runbooks"]:
        if any(r["incident_type"] in m["incident_types"] for m in bundle["monitoring"]):
            lines.append(f"- [{r['id']}] 조회한 런북: {r['incident_type']}" + (f" / {r['uri']}" if r["uri"] else ""))
    for m in bundle["monitoring"]:
        for r in bundle["runbooks"]:
            if r["incident_type"] not in m["incident_types"]:
                continue
            for step in r["steps"]:
                # 제공 런북의 두 가지 명시적 수치 조건만 해석한다. 지속 시간·조치 권한은 별도다.
                cpu = re.search(r"CPU가 (\d+(?:\.\d+)?)%를 넘고 지속되면", step["text"])
                memory = re.search(r"OOM 직전\((\d+(?:\.\d+)?)% 이상\)", step["text"])
                if cpu or memory:
                    threshold = float((cpu or memory).group(1))
                    value = m["snapshot"]["cpu_percent" if cpu else "memory_percent"]
                    met = value > threshold if cpu else value >= threshold
                    label, symbol = ("CPU", ">") if cpu else ("메모리", "≥")
                    lines.append(f"- [{m['id']} → {step['id']}] 런북 수치 조건: {label} {value:g}% {symbol} "
                                 f"{threshold:g}% → {'충족' if met else '불충족'}. "
                                 "지속 시간·기타 조치 조건은 미확인입니다.")
    steps = {s["id"]: (r["incident_type"], s["text"]) for r in bundle["runbooks"] for s in r["steps"]}
    for a in draft.recommendations:
        kind, text = steps[a.step_id]
        lines.append(f"- [{a.evidence_id} → {a.step_id}] {kind} 런북 원문: {text}")
    if not draft.recommendations:
        lines.append("- 현재 근거에 적용할 런북 절차를 선택하지 않았습니다. 증상과 자료를 추가 확인하세요.")
    missing_types = sorted({t for m in bundle["monitoring"] for t in m["incident_types"]}
                           - {r["incident_type"] for r in bundle["runbooks"] if r["steps"]})
    if missing_types:
        lines.append("- 필요한 런북을 확보하지 못했습니다: " + ", ".join(missing_types))
    lines.append("\n확인 범위: MCP 모니터링 조회와 표시된 런북 조회. 로그·배포 이력·트래픽·힙 덤프는 수집하지 않았습니다. "
                 "재시작·확장·롤백 등 조치는 실행하지 않았으며, 런북의 조건 충족·지속 시간은 추가 확인해야 합니다. "
                 "알림 원문은 알림 시점의 정보이며 현재 수치와 다를 수 있습니다.")
    return "\n".join(lines)
