"""수집 근거 → 구조화된 모델 제안 → 코드 검증 → 답변 렌더링.

모델이 생성한 자유 문장과 수치를 최종 답변에 직접 사용하지 않는다.
검증된 관측의 의미와 런북의 점검 방향을 먼저 설명하고, 수치·출처로 뒷받침한다.
강의의 Pydantic 스키마, 권위 게이트, Validator를 적용한 제한된 작성 방식이다.
"""
from __future__ import annotations

import hashlib
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
RUNBOOK_EXPLANATION = {
    "cpu-spike": "요청·트래픽 변화나 반복 호출, 배치 작업 중복처럼 CPU를 소모하는 작업이 있는지 확인하는 방향입니다. 최근 로그와 배포·트래픽 자료를 대조해야 원인을 좁힐 수 있습니다.",
    "memory-leak": "일시적인 사용량 증가와 메모리 누수를 구분하는 방향입니다. 사용량이 계속 증가하는지 추세를 보고, 힙 덤프와 최근 배포 내용을 확인해야 합니다.",
    "high-latency": "커넥션 풀 대기나 느린 쿼리·외부 호출이 지연과 관련되는지 확인하는 방향입니다. 지연이 특정 인스턴스에 몰리는지도 점검해야 합니다.",
    "network-timeout": "서비스 간 호출의 어느 구간에서 실패하는지 좁히는 방향입니다. DNS·방화벽·로드밸런서 설정의 최근 변경과 실제 타임아웃 구간을 대조해야 합니다.",
    "disk-full": "저장 공간 부족과 로그 등의 비정상 증가를 확인하는 방향입니다. 실제 디스크 사용률과 공간을 많이 차지하는 항목을 확인한 뒤 정리·확장을 검토해야 합니다.",
    "db-down": "커넥션 풀과 리플리케이션 상태를 확인해 연결 문제와 DB 의존성 장애를 구분하는 방향입니다. 전환이 필요한지는 점검 결과에 따라 판단해야 합니다.",
}
# 제공 런북의 전체 절차가 일치할 때만 미리 검토한 해설을 연결한다.
# 문서가 달라지면 해당 해설을 생략하고 실제 조회한 절차를 그대로 제공한다.
RUNBOOK_DIGEST = {
    "cpu-spike": "c03c20b6f29e43cc41a8477482f1ff6182e8114ebf19ffd701e10312f19e76ca",
    "memory-leak": "2ad0024d8860645f6763fdf398b2e74e069b7a22444da1a6168bf97eadf7501c",
    "high-latency": "3a118eec0fdedd4b7dca694bfc844b177165e683fe56016e40fb4ff93c8cee12",
    "network-timeout": "7b85df05b517ea66cd8971012a336736310bf57733581009acd62dd3fa510e7c",
    "disk-full": "6d087ca02cf63734d387918962c28f1d6bd216e4df2cfb17d4438d762f5bdad7",
    "db-down": "3d5f94640554e5b3084c90593af898c774a0f9c975fc659f9fde9cb1f9e97f78",
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
    return {"monitoring": monitoring, "runbooks": runbooks, "unavailable": unavailable,
            "policy": policy.model_dump()}


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
        "allowed_step_ids가 있으면 recommendations에 evidence_id와 해당 step_id를 선택하세요. "
        "각 서비스에서 원인을 좁히는 현재 점검 절차를 먼저 선택하고, 관련 항목을 세 개 이내로 우선순위대로 고르세요. "
        "정상화 후 회고보다 현재 확인할 절차가 우선입니다. 조건부 조치를 이미 수행한 것으로 취급하지 마세요. "
        "서비스별 허용 목록을 지키세요. 이 선택은 원인 확정이나 조치 실행을 뜻하지 않습니다. "
        "스키마: " + json.dumps(writer_schema(bundle), ensure_ascii=False))
    user = json.dumps({"query": query, "evidence": bundle, "validation_feedback": feedback}, ensure_ascii=False)
    return system, user


def explain_observation(source: dict, policy: RecoveryPolicy) -> str:
    """실측과 정책으로 말할 수 있는 상태 해석. 근본 원인·실제 장애 영향은 확정하지 않는다."""
    s = source["snapshot"]
    cpu_high, memory_high = s["cpu_percent"] >= policy.cpu_limit, s["memory_percent"] >= policy.memory_limit
    state = {"healthy": "정상", "degraded": "성능 저하", "down": "중단"}[s["status"]]
    sentences = [f"{s['system_name']}는 모니터링상 {state}({s['status']}) 상태입니다."]
    if cpu_high:
        sentences.append(f"CPU {s['cpu_percent']:g}%는 정상 기준({policy.cpu_limit:g}% 미만)을 벗어나 CPU 부하를 우선 점검할 근거가 있습니다.")
    if memory_high:
        sentences.append(f"메모리 {s['memory_percent']:g}%도 정상 기준({policy.memory_limit:g}% 미만)을 벗어나 메모리 사용 추세를 함께 확인해야 합니다." if cpu_high else
                         f"메모리 {s['memory_percent']:g}%는 정상 기준({policy.memory_limit:g}% 미만)을 벗어나 메모리 사용 추세를 우선 확인해야 합니다.")
    if cpu_high and not memory_high:
        sentences.append(f"메모리 {s['memory_percent']:g}%는 정상 기준 안에 있어, 이번 관측의 자원 수치에서는 CPU 쪽 이상이 두드러집니다.")
    elif memory_high and not cpu_high:
        sentences.append(f"CPU {s['cpu_percent']:g}%는 정상 기준 안에 있어, 이번 관측의 자원 수치에서는 메모리 쪽 이상이 두드러집니다.")
    elif not cpu_high and not memory_high:
        sentences.append(f"CPU {s['cpu_percent']:g}%와 메모리 {s['memory_percent']:g}%는 설정한 정상 기준 안에 있습니다.")
    if s["alerts"]:
        sentences.append(f"다만 활성 알림 {len(s['alerts'])}건이 남아 있어 알림 내용과 실제 증상을 대조해야 합니다." if not cpu_high and not memory_high else
                         f"활성 알림 {len(s['alerts'])}건도 함께 확인해야 합니다.")
    elif s["status"] == "healthy" and not cpu_high and not memory_high:
        sentences.append("현재 조회한 항목에서는 이상 신호가 없지만, 이 한 번의 관측만으로 실제 장애 복구까지 확정하지는 않습니다.")
    elif s["status"] != "healthy" and not cpu_high and not memory_high:
        sentences.append("자원 수치가 정상이어도 서비스 상태는 비정상이므로, 의존성·응답 상태를 추가 점검해야 합니다.")
    return " ".join(sentences) + f" [{source['id']}]"


def runbook_conditions(bundle: dict) -> list[dict]:
    """런북의 명시적인 CPU/메모리 수치 조건만 판정한다. 지속 시간 등은 미확인이다."""
    conditions = []
    for m in bundle["monitoring"]:
        for r in bundle["runbooks"]:
            if r["incident_type"] not in m["incident_types"]:
                continue
            for step in r["steps"]:
                cpu = re.search(r"CPU가 (\d+(?:\.\d+)?)%를 넘고 지속되면", step["text"])
                memory = re.search(r"OOM 직전\((\d+(?:\.\d+)?)% 이상\)", step["text"])
                if cpu or memory:
                    threshold = float((cpu or memory).group(1))
                    value = m["snapshot"]["cpu_percent" if cpu else "memory_percent"]
                    conditions.append({"source": m["id"], "step": step["id"], "cpu": bool(cpu),
                                       "value": value, "threshold": threshold,
                                       "met": value > threshold if cpu else value >= threshold})
    return conditions


def render_answer(bundle: dict, draft: WriterDraft, status: str) -> str:
    lines = ["## 진단 요약"]
    if status == "fallback":
        lines.append("검증 보류: 모델 제안을 채택하지 않고 확인된 근거와 참고 절차만 제공합니다.")
    elif status == "partial":
        lines.append("일부 자료가 부족합니다. 확보된 근거만 검증했으며 누락된 대상·절차는 판단을 보류합니다.")
    policy = RecoveryPolicy.model_validate(bundle.get("policy", {}))
    for m in bundle["monitoring"]:
        lines.append(explain_observation(m, policy))
    for missing in bundle["unavailable"]:
        lines.append(f"{missing['system_name']}: 판단 보류 — {missing['reason']}")
    if draft.hypotheses:
        lines.append("\n## 가능한 원인과 우선 점검 방향 (미확정)")
        for h in draft.hypotheses:
            m = next(m for m in bundle["monitoring"] if m["id"] == h.evidence_id)
            relevant = [r for r in bundle["runbooks"] if r["incident_type"] in m["incident_types"]
                        and RUNBOOK_KIND[r["incident_type"]] == h.kind and r["steps"]]
            lines.append(f"- {m['snapshot']['system_name']}: {CAUSE_TEXT[h.kind]} [{h.evidence_id}]")
            # 설명은 실제 조회한 제공 런북과 내용이 같은 경우에만 연결한다.
            for r in relevant:
                digest = hashlib.sha256("\n".join(step["text"] for step in r["steps"]).encode()).hexdigest()
                if digest == RUNBOOK_DIGEST[r["incident_type"]]:
                    lines.append(f"  {RUNBOOK_EXPLANATION[r['incident_type']]} [{h.evidence_id} → {r['id']}]")
        lines.append("위 내용은 확인할 원인 후보입니다. 해당 로그·추세·설정 자료를 아직 확보하지 않았으므로 원인으로 단정하지 않습니다.")
    lines.append("\n## 다음 확인 순서·조치 판단 (미실행)")
    steps = {s["id"]: (r["incident_type"], s["text"]) for r in bundle["runbooks"] for s in r["steps"]}
    conditions = runbook_conditions(bundle)
    numeric_conditions = {(c["source"], c["step"]): c for c in conditions}
    for i, a in enumerate(draft.recommendations, 1):
        kind, text = steps[a.step_id]
        condition = numeric_conditions.get((a.evidence_id, a.step_id))
        if condition and not condition["met"]:
            stage = "현재 수치 조건 미충족으로 보류할 조치"
        elif "정상화 후" in text or "정상화 확인 후" in text:
            stage = "정상화 후 후속 작업"
        elif re.search(r"늘리|확장|띄워|재시작|롤백|정리해|전환|제외|상향|분산", text):
            stage = "조건·영향 확인 후 검토할 조치"
        else:
            stage = "먼저 확인할 사항"
        lines.append(f"{i}) [{a.evidence_id} → {a.step_id}] {stage}: {text}")
    if not draft.recommendations:
        lines.append("현재 자료로 적용할 런북 절차를 선택하지 않았습니다. 요청한 증상을 추가 확인하고 새 관측으로 재점검하세요.")
    for c in conditions:
        label, symbol = ("CPU", ">") if c["cpu"] else ("메모리", "≥")
        lines.append(f"- [{c['source']} → {c['step']}] 런북 수치 조건: {label} {c['value']:g}% {symbol} "
                     f"{c['threshold']:g}% → {'충족' if c['met'] else '불충족'}. 지속 시간·기타 조치 조건은 미확인입니다.")
        if c["cpu"]:
            lines.append("  수치 조건은 충족했으므로 고부하가 지속되는지와 증설 영향을 확인한 뒤 부하 분산을 검토하세요. 즉시 증설을 실행했다는 뜻은 아닙니다." if c["met"] else
                         "  이 수치만으로는 런북의 증설 조건을 충족하지 않습니다. 먼저 요청·작업 패턴을 점검하고 CPU 추세를 재확인하세요.")
        else:
            lines.append("  수치 조건은 충족했지만 실제 OOM 위험과 재시작 영향을 확인한 뒤 임시 조치를 결정해야 합니다. 메모리 누수 원인이 확인된 것은 아닙니다." if c["met"] else
                         "  이 수치만으로는 런북의 재시작 수치 조건을 충족하지 않습니다. 사용량 추세와 힙 자료로 원인을 먼저 좁히세요.")
    lines.append("\n## 확인된 모니터링 근거")
    for m in bundle["monitoring"]:
        s = m["snapshot"]
        lines += [f"- [{m['id']}] {s['system_name']}: 상태 {s['status']}, CPU {s['cpu_percent']:g}%, "
                  f"메모리 {s['memory_percent']:g}%, 알림 {len(s['alerts'])}건.",
                  f"  관측 시각 {s['last_checked']} / 관측 ID {s['sample_id']} / 자료 {s['data_source']}"]
        if s["alerts"]:
            lines.append("  모니터링 알림 원문: " + "; ".join(s["alerts"]))
    for missing in bundle["unavailable"]:
        lines.append(f"- {missing['system_name']}: 판단 보류 — {missing['reason']}")
    for r in bundle["runbooks"]:
        if any(r["incident_type"] in m["incident_types"] for m in bundle["monitoring"]):
            lines.append(f"- [{r['id']}] 조회한 런북: {r['incident_type']}" + (f" / {r['uri']}" if r["uri"] else ""))
    missing_types = sorted({t for m in bundle["monitoring"] for t in m["incident_types"]}
                           - {r["incident_type"] for r in bundle["runbooks"] if r["steps"]})
    if missing_types:
        lines.append("- 필요한 런북을 확보하지 못했습니다: " + ", ".join(missing_types))
    lines.append("\n확인 범위: MCP 모니터링 조회와 표시된 런북 조회. 로그·배포 이력·트래픽·힙 덤프는 수집하지 않았습니다. "
                 "재시작·확장·롤백 등 조치는 실행하지 않았으며, 런북의 조건 충족·지속 시간은 추가 확인해야 합니다. "
                 "알림 원문은 알림 시점의 정보이며 현재 수치와 다를 수 있습니다.")
    return "\n".join(lines)
