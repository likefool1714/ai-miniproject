"""LangGraph 기능 노드: 문맥 해석 → MCP 재점검 → 이력/복구 판정 → 보고서."""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlencode

from langsmith import traceable
from pydantic import ValidationError

from .recovery import PHASE_LABELS, render_assessment
from .retry import with_retry

KNOWN_SYSTEMS = tuple(json.loads((Path(__file__).resolve().parent.parent / "data/monitoring.json").read_text()))
ALIASES = {"인증 서비스": "auth-service", "결제 서비스": "payments-service",
           "캐시 서비스": "cache-service", "게이트웨이 서비스": "gateway-service",
           "로그 서비스": "log-service", "웹 서버": "web-01"}
SERVICE_PATTERN = re.compile(r"(?<![a-z0-9_-])[a-z][a-z0-9]*(?:-[a-z0-9]+)*-(?:service|\d+)(?![a-z0-9_-])", re.I)
OPERATIONAL_PATTERN = re.compile(
    r"장애|진단|느려|느리|지연|모니터링|조사|점검|CPU|메모리|상태|다운|"
    r"타임아웃|timeout|저하|원인|분석|오류|에러|보고서\s*형식|마크다운\s*형식|markdown\s*형식", re.I)


def _target_mentions(query: str) -> list[tuple[int, int, str]]:
    mentioned = [(m.start(), m.end(), m.group().lower()) for m in SERVICE_PATTERN.finditer(query)]
    for alias, system in ALIASES.items():
        pattern = r"\s*".join(map(re.escape, alias.split()))
        mentioned.extend((m.start(), m.end(), system) for m in re.finditer(pattern, query))
    return sorted(mentioned)


def extract_targets(query: str) -> tuple[list[str], list[str]]:
    mentioned = _target_mentions(query)
    excluded = set()
    exclusion = re.compile(
        r"^\s*(?:[은는을를]\s*)?(?:말고|대신|빼고|"
        r"제외(?:하고|한|해(?:\s*줘|\s*주세요)?|(?=\s|[,.!?]|$))|"
        r"(?:조사|확인|점검)\s*하지\s*말고)")
    for index, (_, end, _) in enumerate(mentioned):
        following = query[end:mentioned[index + 1][0] if index + 1 < len(mentioned) else len(query)]
        if exclusion.search(following):
            excluded.add(index)
            # 'A와 B는 제외하고 C'처럼 연결된 제외 대상도 함께 제거한다.
            previous = index - 1
            while previous >= 0 and re.fullmatch(
                    r"\s*(?:와|과|및|하고|,|·)\s*",
                    query[mentioned[previous][1]:mentioned[previous + 1][0]]):
                excluded.add(previous)
                previous -= 1
    ordered = list(dict.fromkeys(system for i, (_, _, system) in enumerate(mentioned) if i not in excluded))
    return [s for s in ordered if s in KNOWN_SYSTEMS], [s for s in ordered if s not in KNOWN_SYSTEMS]


def _target_answer(query: str) -> bool:
    """대기 중인 확인 질문의 짧은 대상 답변과 새 전체 명령을 구분한다."""
    remaining = query
    for start, end, _ in reversed(_target_mentions(query)):
        remaining = remaining[:start] + remaining[end:]
    remaining = re.sub(r"[\s,.!?]", "", remaining)
    return bool(re.fullmatch(
        r"(?:의)?(?:상태)?(?:요|이요|입니다|이에요|예요|에요|로요|으로요)?", remaining))


def command(query: str) -> str:
    # '복구 방법/원인 분석'은 새 진단이고, '복구됐어?'는 상태 재점검이다.
    # '보고서 형식'은 출력 형식 요청이지 저장 기록의 파일 내보내기가 아니다.
    diagnostic = bool(re.search(
        r"원인|분석|방법|절차|어떻게|로그.{0,20}(?:조사|확인|점검)", query, re.I))
    formatted = bool(re.search(r"(?:보고서|markdown|마크다운)\s*형식", query, re.I))
    if diagnostic:
        return "legacy"
    if not formatted and re.search(r"보고서|내보내|다운로드|markdown|마크다운", query, re.I):
        return "report"
    if re.search(r"이력(?!서)|히스토리|조사\s*기록|조회\s*기록", query):
        return "history"
    if not formatted and re.search(r"복구|회복|재점검|재발|다시.{0,12}(확인|조사|점검|봐)|재확인|상태.{0,8}(비교|변화)|이전.{0,8}비교", query):
        return "recheck"
    return "legacy"


def completed(state, reply, event="RecoveryCompleted", **fields):
    return {"final": reply, "events": state.get("events", []) + [{"type": event, **fields}]}


@traceable(name="request_context_node")
async def request_context_node(state, deps):
    if deps.recovery_store is None:
        targets, _ = extract_targets(state["query"])
        return {"recovery_action": "legacy", "resolved_targets": targets,
                "operational_request": bool(OPERATIONAL_PATTERN.search(state["query"]))}
    store = deps.recovery_store
    query, thread = state["query"], state["thread_id"]
    context = store.context(thread)
    action = command(query)
    targets, unknown = extract_targets(query)
    pending = context.get("pending")
    if re.fullmatch(r"\s*(취소|그만|취소해\s*줘)[.!?]?\s*", query):
        store.set_context(thread, pending=None)
        return {"recovery_action": "clarify", **completed(state, "확인 대기 중인 요청을 취소했습니다.", "ClarificationCancelled")}
    if pending and targets and not unknown and action == "legacy" and _target_answer(query):
        action = pending["action"]
        query = f"{pending['query']}\n확인된 대상: {', '.join(targets)}"
    elif pending and (action != "legacy" or (targets and OPERATIONAL_PATTERN.search(query))):
        # 사용자가 새 명령을 보냈으면 이전 확인 질문을 이어붙이지 않는다.
        store.set_context(thread, pending=None)
    mentioned = _target_mentions(query)
    operational = (bool(unknown) or action != "legacy" or bool(OPERATIONAL_PATTERN.search(query))
                   or bool(mentioned and not targets))
    explicit_reference = bool(re.search(r"그\s*서비스|그\s*시스템|이전\s*서비스|방금", query))
    if not targets and not unknown and not mentioned and (operational or explicit_reference):
        last_targets = context.get("targets", [])
        if len(last_targets) == 1 or re.search(r"모두|전부|전체|둘\s*다", query):
            targets = last_targets
    if operational and (unknown or not targets):
        reason = f"미등록 서비스: {', '.join(unknown)}. " if unknown else "조사할 서비스를 확정하지 못했습니다. "
        store.set_context(thread, pending={"action": action, "query": state["query"]})
        return {"recovery_action": "clarify", **completed(state, reason + "어떤 서비스를 확인할까요?\n등록 서비스: " + ", ".join(KNOWN_SYSTEMS), "ClarificationRequired")}
    if targets and operational:
        store.set_context(thread, targets=targets, pending=None)
    events = state.get("events", [])
    if action != "legacy":
        events = events + [{"type": "RouteDecided", "route": action}, {"type": "TargetsResolved", "targets": targets}]
    if action == "legacy" and targets and explicit_reference:
        query += "\n조사 대상: " + ", ".join(targets)
    return {"query": query, "resolved_targets": targets, "recovery_action": action, "events": events,
            "operational_request": operational}


@traceable(name="recovery_collect_node")
async def recovery_collect_node(state, deps):
    async def fetch(system):
        try:
            data, retries = await asyncio.wait_for(with_retry(deps.mcp.call_tool, "get_monitoring_status", {"system_name": system, "observation_context": state["thread_id"]}), timeout=15)
            if data.get("system_name") != system:
                return system, None, "요청한 서비스와 응답 대상이 다릅니다.", retries
            return system, data, None, retries
        except asyncio.CancelledError:
            # 전체 요청 한도/연결 취소도 '수집 중이던 대상'의 연속성을 끊는다.
            # 잠금 대기 중이거나 이미 끝난 조사는 이 경로에 들어오지 않는다.
            deps.recovery_store.failed(state["thread_id"], system, "모니터링 수집이 취소되어 최신 상태를 확인하지 못했습니다.")
            raise
        except Exception as exc:
            return system, None, f"모니터링 조회 실패 ({type(exc).__name__})", 0
    results = await asyncio.gather(*(fetch(s) for s in state["resolved_targets"]))
    collected, failures, events = [], [], list(state["events"])
    for system, data, error, retries in results:
        if error:
            failures.append({"system_name": system, "error": error})
            deps.recovery_store.failed(state["thread_id"], system, error)
            events.append({"type": "ObservationFailed", "target": system, "reason": error})
        else:
            collected.append({"source": "monitoring", "data": data})
            events.append({"type": "TaskCompleted", "source": "monitoring", "target": system, "retries": retries})
    return {"collected": collected, "recovery_failures": failures, "events": events,
            "retry_count": sum(r[3] for r in results)}


@traceable(name="recovery_assess_node")
async def recovery_assess_node(state, deps):
    if deps.recovery_store is None:
        return {}
    results, failures = [], list(state.get("recovery_failures", []))
    events = list(state["events"])
    # 재계획으로 같은 대상이 여러 번 조회됐으면 요청 내 마지막 관측만 저장한다.
    monitoring = {c["data"]["system_name"]: c["data"] for c in state.get("collected", []) if c["source"] == "monitoring"}
    for system, payload in monitoring.items():
        try:
            result = deps.recovery_store.record(state["thread_id"], payload)
            results.append(result)
            a = result["assessment"]
            events.append({"type": "RecoveryAssessed", "target": system, "phase": a["phase"], "label": PHASE_LABELS[a["phase"]], "recurrence": a["recurrence"], "observation_id": result["id"]})
        except (ValidationError, ValueError, KeyError) as exc:
            error = f"관측 자료 형식 오류 ({type(exc).__name__}); 복구 판단 보류"
            deps.recovery_store.failed(state["thread_id"], system, error)
            failures.append({"system_name": system, "error": error})
            events.append({"type": "ObservationFailed", "target": system, "reason": error})
    return {"recovery_results": results, "recovery_failures": failures, "events": events}


def recovery_text(state) -> str:
    blocks = [render_assessment(r["snapshot"]["system_name"], r["snapshot"], r["assessment"]) for r in state.get("recovery_results", [])]
    blocks += [f"### {f['system_name']}: 판단 보류\n- {f['error']}" for f in state.get("recovery_failures", [])]
    if not blocks:
        return "저장하거나 비교할 유효한 관측이 없습니다. 복구 판단을 보류합니다."
    return "\n\n".join(blocks) + "\n\n※ 샘플 데이터와 설정한 정책에 따른 판정입니다. 실제 서비스 복구·원인 확정을 보장하지 않습니다."


@traceable(name="recovery_answer_node")
async def recovery_answer_node(state, deps):
    return completed(state, recovery_text(state), "Finalize", pipeline="recovery")


@traceable(name="recovery_history_node")
async def recovery_history_node(state, deps):
    blocks = []
    for system in state["resolved_targets"]:
        rows = deps.recovery_store.history(state["thread_id"], system, 10)
        lines = [f"### {system} 조사 이력 (최근 {len(rows)}건)"]
        for row in rows:
            s, a = row["snapshot"], row["assessment"]
            label = PHASE_LABELS[a["phase"]] + (" / 재발" if a["recurrence"] else "")
            lines.append(f"- {s['last_checked']}: 상태 {s['status']}, CPU {s['cpu_percent']:g}%, 메모리 {s['memory_percent']:g}%, 알림 {len(s['alerts'])}건 → 당시 판정 {label}")
        if not rows:
            lines.append("- 이 대화에서 저장된 조사 기록이 없습니다.")
        blocks.append("\n".join(lines))
    return completed(state, "\n\n".join(blocks) + "\n\n이력 조회는 새 모니터링을 실행하지 않습니다. 당시 판정은 현재 복구 상태를 뜻하지 않습니다.", "HistoryLoaded")


@traceable(name="recovery_report_node")
async def recovery_report_node(state, deps):
    blocks, urls = [], []
    for system in state["resolved_targets"]:
        report = deps.recovery_store.report(state["thread_id"], system)
        if report:
            urls.append({"system_name": system, "url": "/reports/markdown?" + urlencode({"thread_id": state["thread_id"], "system_name": system})})
            blocks.append(report)
        else:
            blocks.append(f"{system}: 이 대화에 조사 기록이 없어 보고서를 만들 수 없습니다.")
    return {"report_links": urls, **completed(state, "\n\n".join(blocks), "ReportReady", reports=urls)}
