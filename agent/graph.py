"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | incident 파이프라인)
incident: plan(MCP Prompt 절차 반영) → gate(권위 게이트) → research(가드+병렬+재시도)
          → replan_check(부족 시 gap task 후 재조사) → analysis(needs_analysis=True일 때만
          실제 실행) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
import json
import time
from functools import partial
from pathlib import Path
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from .deps import SupervisorDeps
from .retry import with_retry
from .grounding import build_bundle, validate_draft, render_answer, fallback_draft, WriterDraft
from .recovery_nodes import (
    request_context_node, recovery_collect_node, recovery_assess_node,
    recovery_answer_node, recovery_history_node, recovery_report_node, recovery_text,
)


def _load_known_systems() -> set[str]:
    """ollama_llm.py/openai_llm.py와 동일한 소스(data/monitoring.json)에서
    시스템 목록을 읽는다 — plan_node가 MCP Prompt에 넘길 system_name을
    쿼리에서 멤버십 매칭으로 뽑아낼 때 쓴다."""
    path = Path(__file__).resolve().parent.parent / "data" / "monitoring.json"
    try:
        return set(json.loads(path.read_text()).keys())
    except (OSError, json.JSONDecodeError):
        return {"auth-service", "web-01"}


KNOWN_SYSTEMS = _load_known_systems()


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    validation_feedback: str
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]
    resolved_targets: list[str]
    recovery_action: str
    recovery_results: list[dict]
    recovery_failures: list[dict]
    report_links: list[dict]
    evidence_bundle: dict
    validated_draft: dict
    validation_status: str
    validation_errors: list[str]
    writer_error: str
    operational_request: bool


def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    events = list(state.get("events", []))
    try:
        result, usage = await asyncio.to_thread(deps.fast_llm.route, state["query"])
    except Exception as exc:
        if not state.get("resolved_targets") or not state.get("operational_request"):
            return {"route": "clarify", "final": "요청 분류 모델의 응답을 받지 못했습니다. 서비스를 명시해 다시 요청해 주세요.",
                    "events": events + [{"type": "ModelFallback", "stage": "route", "reason": type(exc).__name__}]}
        result, usage = {"route": "incident", "needs_analysis": False}, {"usd": 0}
        events.append({"type": "ModelFallback", "stage": "route", "reason": type(exc).__name__})
    if state.get("resolved_targets") and state.get("operational_request"):
        result = {"route": "incident", "needs_analysis": result.get("needs_analysis", False)}
    if result["route"] == "incident" and not state.get("resolved_targets"):
        # 라우터가 운영 요청으로 판단한 경우에도 임의의 기본 서비스를 조사하지 않는다.
        if deps.recovery_store is not None:
            deps.recovery_store.set_context(state["thread_id"], pending={"action": "legacy", "query": state["query"]})
        return {"route": "clarify", "final": "어떤 서비스를 조사할까요? 등록 서비스: " + ", ".join(sorted(KNOWN_SYSTEMS)),
                "cost_usd": _cost(state, usage), "events": state.get("events", []) + [{"type": "ClarificationRequired"}]}
    if result["route"] == "incident" and deps.recovery_store is not None:
        deps.recovery_store.set_context(state["thread_id"], targets=state["resolved_targets"], pending=None)
    event = {"type": "RouteDecided", "route": result["route"]}
    return {"route": result["route"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage),
            "events": events + [event]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
    procedure = await deps.mcp.get_prompt(
        "incident_analysis_prompt",
        {"system_name": state["resolved_targets"][0], "symptom": state["query"]},
        server=prompt_server,
    )
    events = list(state["events"])
    try:
        result, usage = await asyncio.to_thread(deps.fast_llm.plan, state["query"], procedure)
    except Exception as exc:
        result = {"tasks": [], "needs_analysis": False}
        usage = {"usd": 0}
        events.append({"type": "ModelFallback", "stage": "plan", "reason": type(exc).__name__})
    # 모델이 문맥 대상을 덮어쓰거나 요청한 복수 서비스를 빠뜨리지 않도록 보정한다.
    if state.get("resolved_targets"):
        targets = state["resolved_targets"]
        others = [t for t in result["tasks"] if t.get("agent") != "research"]
        result["tasks"] = [{"id": f"task-{i}", "agent": "research", "kind": "monitoring",
                            "target": s, "status": "pending"} for i, s in enumerate(targets)] + others
    # 모델이 계획한 별도 runbook은 관측 전 추측이므로 실행하지 않는다.
    targets = [t["target"] for t in result["tasks"]
               if t["agent"] == "research" and t.get("target")]
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets}
    return {"tasks": result["tasks"], "needs_analysis": result["needs_analysis"],
            "cost_usd": _cost(state, usage), "events": events + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 15 가드 + Wave 10b 병렬 + Wave 9 재시도 + Wave 2 Tool/Resource 실호출."""
    tasks = state["tasks"]
    research_indices = [i for i, t in enumerate(tasks)
                        if t["agent"] == "research" and t["status"] == "pending"]
    # Wave 15 — 객체가 아니라 '실행할 목록'을 검사한다 (not research_agent 버그의 수정판)
    if not research_indices:
        return {"events": state["events"] +
                [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}

    retry_total = state.get("retry_count", 0)

    # URI를 코드에 하드코딩하지 않는다. 여러 MCP 서버가 연결돼 있고 그중 어느 서버가
    # "get_incident_runbook" resource를 제공하는지 모른다고 가정하고, 먼저 연결된
    # 서버들을 조회(list)해 유효한 서버를 찾은 뒤에만 그 서버로 요청한다.
    needs_runbook = any(tasks[i]["kind"] != "monitoring" for i in research_indices)
    runbook_server = runbook_uri_template = None
    if needs_runbook:
        runbook_server, runbook_uri_template = await deps.mcp.find_resource_template(
            "get_incident_runbook")

    async def run_one(idx: int) -> dict:
        task = tasks[idx]
        await asyncio.sleep(0.35)   # 태스크당 고정 지연 — 병렬 효과를 실측하기 위함
        if task["kind"] == "monitoring":
            arguments = {"system_name": task["target"]}
            if deps.recovery_store is not None:
                arguments["observation_context"] = state["thread_id"]
            try:
                data, retries = await asyncio.wait_for(with_retry(
                    deps.mcp.call_tool, "get_monitoring_status",
                    arguments), timeout=15)
            except asyncio.CancelledError:
                if deps.recovery_store is not None:
                    deps.recovery_store.failed(
                        state["thread_id"], task["target"],
                        "모니터링 수집이 취소되어 최신 상태를 확인하지 못했습니다.")
                raise
            if data.get("system_name") != task["target"]:
                raise ValueError("모니터링 응답 대상이 요청한 서비스와 다릅니다.")
            return {"idx": idx, "source": "monitoring", "data": data, "retries": retries}
        uri = runbook_uri_template.format(incident_type=task["target"])
        data = await deps.mcp.read_resource(uri, server=runbook_server)
        return {"idx": idx, "source": "runbook", "data": data, "uri": uri, "retries": 0}

    started = time.perf_counter()
    results = await asyncio.gather(*(run_one(i) for i in research_indices), return_exceptions=True)
    elapsed = round(time.perf_counter() - started, 2)

    new_tasks = [dict(t) for t in tasks]
    collected = list(state.get("collected", []))
    events = list(state["events"])
    failures = list(state.get("recovery_failures", []))
    for idx, r in zip(research_indices, results):
        if isinstance(r, Exception):
            task = new_tasks[idx]
            task["status"] = "failed"
            reason = f"자료 조회 실패 ({type(r).__name__})"
            events.append({"type": "TaskFailed", "target": task["target"], "reason": reason})
            if task["kind"] == "monitoring":
                failures.append({"system_name": task["target"], "error": reason})
                if deps.recovery_store is not None:
                    deps.recovery_store.failed(state["thread_id"], task["target"], reason)
            continue
        new_tasks[r["idx"]]["status"] = "done"
        collected.append({"source": r["source"], "data": r["data"], "target": new_tasks[r["idx"]]["target"],
                          "uri": r.get("uri")})
        retry_total += r["retries"]
        events.append({"type": "TaskCompleted", "id": new_tasks[r["idx"]]["id"],
                       "source": r["source"], "target": new_tasks[r["idx"]]["target"],
                       "retries": r["retries"]})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": elapsed, "events": events, "recovery_failures": failures}


def _bundle(state: AgentState, deps: SupervisorDeps) -> dict:
    store = deps.recovery_store
    bundle = build_bundle(state.get("collected", []), state.get("resolved_targets", []),
                          store.policy if store else None, store.clock() if store else None)
    rejected = {r["snapshot"]["system_name"]: r["assessment"]["reasons"][0]
                for r in state.get("recovery_results", []) if r.get("usable") is False}
    bundle["monitoring"] = [m for m in bundle["monitoring"] if m["snapshot"]["system_name"] not in rejected]
    already_missing = {m["system_name"] for m in bundle["unavailable"]}
    bundle["unavailable"] += [{"system_name": s, "reason": reason} for s, reason in rejected.items() if s not in already_missing]
    return bundle


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 7 — 관측에 맞는 런북만 한 차례 추가 조회. 정상 상태에는 기본 런북 없음."""
    bundle = _bundle(state, deps)
    available = {r["incident_type"] for r in bundle["runbooks"]}
    required = {t for m in bundle["monitoring"] for t in m["incident_types"]}
    missing = sorted(required - available)
    rounds = state.get("replan_rounds", 0)
    if not missing or rounds >= 1:
        verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": not missing,
                   "stop": True, "missing_runbooks": missing}
        return {"events": state["events"] + [verdict]}
    gap_tasks = [{"id": f"task-gap-{t}", "agent": "research", "kind": "runbook",
                  "target": t, "status": "pending"} for t in missing]
    verdict = {"type": "ReplanCheck", "round": rounds, "is_sufficient": False,
               "target": ", ".join(missing)}
    return {"tasks": state["tasks"] + gap_tasks, "replan_rounds": rounds + 1,
            "events": state["events"] + [verdict]}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    valid_monitoring = [{"source": "monitoring", "data": m["snapshot"]}
                        for m in _bundle(state, deps)["monitoring"]]
    if not valid_monitoring:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "유효한 모니터링 자료 0개"}]}
    try:
        summary, usage = await asyncio.to_thread(deps.fast_llm.analyze, valid_monitoring)
    except Exception as exc:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": f"모델 분석 실패 ({type(exc).__name__}); 수집 근거만 사용"}]}
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """구조화 제안 작성. 검증 이전의 모델 토큰은 화면으로 내보내지 않는다."""
    attempt = state.get("validation_attempts", 0) + 1
    get_stream_writer()({"type": "DraftStarted", "attempt": attempt})
    bundle = _bundle(state, deps)
    error, draft, usage = "", "", {"usd": 0}
    if not bundle["monitoring"]:
        error = "no_valid_observation"
    else:
        try:
            draft, usage = await asyncio.wait_for(deps.quality_llm.write_grounded(
                state["query"], bundle, state.get("validation_feedback")), timeout=40)
            if not isinstance(draft, str):
                draft = ""  # 거부 응답의 null 등은 스키마 반려 후 제한된 재작성으로 처리한다.
        except Exception as exc:
            error = "writer_timeout" if isinstance(exc, TimeoutError) else "writer_unavailable"
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage), "evidence_bundle": bundle, "writer_error": error,
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """스키마·출처·서비스별 적용 가능성을 검증. 재작성은 최대 한 번."""
    bundle = _bundle(state, deps)  # 모델 생성 중 자료가 오래됐는지도 다시 확인한다.
    attempts = state.get("validation_attempts", 0) + 1
    draft, errors = validate_draft(state["draft"], bundle)
    if state.get("writer_error"):
        draft, errors = None, [state["writer_error"]]
    retry = draft is None and not state.get("writer_error") and attempts < 2 and bool(bundle["monitoring"])
    unavailable = bool(bundle["unavailable"]) or any(
        t not in {r["incident_type"] for r in bundle["runbooks"] if r["steps"]}
        for m in bundle["monitoring"] for t in m["incident_types"])
    status = ("partial" if unavailable else "validated") if draft else "retry" if retry else "fallback"
    verdict = ("부분합격" if unavailable else "합격") if draft else "불합격" if retry else "보류"
    feedback = ", ".join(errors) if retry else None
    event = {"type": "Validation", "attempt": attempts, "verdict": verdict, "errors": errors}
    return {"validation_attempts": attempts, "validation_feedback": feedback,
            "validation_status": status,
            "validation_errors": errors, "evidence_bundle": bundle,
            "validated_draft": (draft or fallback_draft(bundle)).model_dump(),
            "events": state["events"] + [event]}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    # 생성 중 자료의 유효기간이 지나면 앞서 저장한 당시 복구 판정도 현재 답변에서는 보류한다.
    unavailable = {m["system_name"]: m["reason"] for m in state["evidence_bundle"]["unavailable"]}
    if unavailable:
        state = dict(state)
        state["recovery_results"] = [
            {**r, "assessment": {**r["assessment"], "phase": "hold", "recurrence": False,
                                  "reasons": [unavailable[r["snapshot"]["system_name"]]]}}
            if r["snapshot"]["system_name"] in unavailable else r
            for r in state.get("recovery_results", [])]
    recovery = "\n\n## 상태 변화·복구 확인 (정책 기반 판정)\n" + recovery_text(state) if state.get("recovery_results") or state.get("recovery_failures") else ""
    answer = render_answer(state["evidence_bundle"], WriterDraft.model_validate(state["validated_draft"]), state["validation_status"])
    return {"final": answer + recovery, "recovery_results": state.get("recovery_results", []),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "incident-v2"}]}


def _after_route(state: AgentState) -> str:
    if state["route"] == "clarify":
        return "clarify"
    return "chat_answer" if state["route"] == "chat" else "plan"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "analysis" if last.get("is_sufficient") or last.get("stop") else "research"


def _after_validate(state: AgentState) -> str:
    return "finalize" if state.get("validation_feedback") is None else "write"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("request_context", node(request_context_node))
    g.add_node("recovery_collect", node(recovery_collect_node))
    g.add_node("recovery_assess", node(recovery_assess_node))
    g.add_node("recovery_answer", node(recovery_answer_node))
    g.add_node("recovery_history", node(recovery_history_node))
    g.add_node("recovery_report", node(recovery_report_node))
    g.add_node("record_observations", node(recovery_assess_node))
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("request_context")
    g.add_conditional_edges("request_context", lambda s: s.get("recovery_action", "legacy"),
                            {"legacy": "route", "recheck": "recovery_collect", "history": "recovery_history",
                             "report": "recovery_report", "clarify": END})
    g.add_edge("recovery_collect", "recovery_assess")
    g.add_edge("recovery_assess", "recovery_answer")
    g.add_edge("recovery_answer", END)
    g.add_edge("recovery_history", END)
    g.add_edge("recovery_report", END)
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "plan": "plan", "clarify": END})
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_edge("gate", "research")
    g.add_edge("research", "replan_check")
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research"})
    g.add_edge("analysis", "record_observations")
    g.add_edge("record_observations", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
