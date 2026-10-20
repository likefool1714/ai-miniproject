import asyncio
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

import server


class RecoveryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "LLM_BACKEND": "fake", "SUPERVISOR_VERSION": "v2", "MONITORING_SCENARIO": "recovery-demo",
            "SESSION_DB_PATH": str(Path(self.temp.name) / "session.db"),
            "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false",
        })
        self.environment.start()
        self.started = asyncio.Event()
        self.shutdown = asyncio.Event()
        async def own_lifespan():
            async with server.lifespan(server.app):
                self.started.set()
                await self.shutdown.wait()
        self.lifespan_task = asyncio.create_task(own_lifespan())
        started_wait = asyncio.create_task(self.started.wait())
        done, _ = await asyncio.wait([self.lifespan_task, started_wait], return_when=asyncio.FIRST_COMPLETED)
        if self.lifespan_task in done:
            await self.lifespan_task
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.shutdown.set()
        await self.lifespan_task
        self.environment.stop()
        self.temp.cleanup()

    async def chat(self, message, thread="test-a"):
        response = await self.client.post("/chat", json={"message": message, "thread_id": thread})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_real_mcp_full_lifecycle_history_and_download(self):
        first = await self.chat("payments-service 장애를 진단해주세요")
        self.assertIn("장애 지속", first["reply"])
        phases = []
        for _ in range(4):
            result = await self.chat("복구됐어? 다시 확인해줘")
            phases.append(result["metadata"]["recovery_results"][0]["assessment"])
        self.assertEqual([a["phase"] for a in phases], ["partial", "partial", "recovered", "ongoing"])
        self.assertTrue(phases[-1]["recurrence"])
        self.assertIn("payments-service", (await self.chat("조사 이력 보여주세요"))["reply"])
        report = await self.chat("보고서 내보내기")
        url = report["metadata"]["reports"][0]["url"]
        downloaded = await self.client.get(url)
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertIn("장애 재발", downloaded.text)
        rows = (await self.client.get("/observations", params={"thread_id": "test-a", "system_name": "payments-service"})).json()["observations"]
        self.assertEqual(len(rows), 5)
        self.assertEqual((await self.client.get("/reports/markdown", params={"thread_id": "other", "system_name": "payments-service"})).status_code, 404)

    async def test_clarification_resume_and_no_unknown_service_lookup(self):
        result = await self.chat("복구됐어?")
        self.assertIn("어떤 서비스", result["reply"])
        resumed = await self.chat("payments-service")
        self.assertEqual(resumed["metadata"]["recovery_results"][0]["snapshot"]["system_name"], "payments-service")
        self.assertEqual(resumed["metadata"]["retry_count"], 1)
        unknown = await self.chat("billing-service 복구됐어?")
        self.assertIn("미등록 서비스", unknown["reply"])
        self.assertEqual(len(server.app.state.deps.recovery_store.history("test-a", "payments-service")), 1)

    async def test_multi_service_context_asks_when_reference_is_ambiguous(self):
        first = await self.chat("payments-service와 cache-service 복구 재점검")
        self.assertEqual(len(first["metadata"]["recovery_results"]), 2)
        ambiguous = await self.chat("그 서비스 복구됐어?")
        self.assertIn("어떤 서비스", ambiguous["reply"])
        both = await self.chat("모두 다시 확인해줘")
        self.assertEqual(len(both["metadata"]["recovery_results"]), 2)

    async def test_sse_uses_same_lifecycle_and_report_links(self):
        first = await self.client.get("/chat/stream", params={"message": "payments-service 복구 재점검", "thread_id": "stream-a"})
        self.assertIn('"type": "RecoveryAssessed"', first.text)
        self.assertIn('"type": "FinalAnswer"', first.text)
        self.assertIn("[DONE]", first.text)
        report = await self.client.get("/chat/stream", params={"message": "보고서 내보내기", "thread_id": "stream-a"})
        events = [json.loads(line[6:]) for line in report.text.splitlines() if line.startswith("data: {")]
        final = next(e for e in events if e["type"] == "FinalAnswer")
        self.assertTrue(final["reports"])

    async def test_history_and_report_do_not_call_monitoring(self):
        await self.chat("payments-service 복구 재점검")
        with patch.object(server.app.state.deps.mcp, "call_tool", side_effect=AssertionError("unexpected monitoring")):
            await self.chat("조사 이력 보여주세요")
            await self.chat("보고서 내보내기")

    async def test_concurrent_requests_keep_one_threads_observations_in_order(self):
        await asyncio.gather(*(self.chat("payments-service 복구 재점검") for _ in range(3)))
        rows = server.app.state.deps.recovery_store.history("test-a", "payments-service")
        self.assertEqual([r["snapshot"]["cpu_percent"] for r in rows], [95, 65, 55])
        self.assertEqual([r["assessment"]["phase"] for r in rows], ["ongoing", "partial", "partial"])

    async def test_bad_tool_result_and_timeout_are_not_recovery(self):
        async def bad(*args, **kw):
            return {"system_name": "payments-service", "status": "healthy", "cpu_percent": 999}
        with patch.object(server.app.state.deps.mcp, "call_tool", bad):
            result = await self.chat("payments-service 복구 재점검")
            self.assertIn("판단 보류", result["reply"])
            self.assertEqual(result["metadata"]["recovery_results"], [])
        async def slow(*args, **kw):
            await asyncio.sleep(2)
        with patch.object(server.app.state.deps.mcp, "call_tool", slow), patch.object(server, "REQUEST_TIMEOUT_SECONDS", 0.02):
            response = await self.client.get("/chat/stream", params={"message": "payments-service 복구 재점검", "thread_id": "timeout"})
            self.assertIn('"type": "Error"', response.text)
            self.assertIn("[DONE]", response.text)
            self.assertNotIn('"phase": "recovered"', response.text)

    async def test_invalid_writer_is_rejected_twice_then_held_without_leaking_draft(self):
        calls = []
        async def invalid(query, bundle, feedback=None):
            calls.append(feedback)
            return "1) 최근 로그를 분석했다. CPU 999%로 확정했다.", {"usd": 0}
        with patch.object(server.app.state.deps.quality_llm, "write_grounded", invalid):
            response = await self.client.get("/chat/stream", params={"message": "payments-service 장애 진단", "thread_id": "invalid-writer"})
        self.assertEqual(len(calls), 2)
        self.assertIn("schema_invalid", calls[1])
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
        self.assertNotIn("AnswerToken", [e["type"] for e in events])
        self.assertEqual([e["verdict"] for e in events if e["type"] == "Validation"], ["불합격", "보류"])
        final = next(e for e in events if e["type"] == "FinalAnswer")
        self.assertEqual(final["validation_status"], "fallback")
        self.assertIn("검증 보류", final["reply"])
        self.assertNotIn("로그를 분석했다", response.text)
        self.assertNotIn("CPU 999%", response.text)

    async def test_writer_can_repair_once_and_pass_evidence_gate(self):
        quality = server.app.state.deps.quality_llm
        original, calls = quality.write_grounded, []
        async def repair(query, bundle, feedback=None):
            calls.append(feedback)
            return ("invalid", {"usd": 0}) if len(calls) == 1 else await original(query, bundle, feedback)
        with patch.object(quality, "write_grounded", repair):
            result = await self.chat("payments-service 장애 진단")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["metadata"]["validation_attempts"], 2)
        self.assertEqual(result["metadata"]["validation_status"], "validated")
        self.assertIn("CPU 95%", result["reply"])

    async def test_writer_unavailable_falls_back_once_without_force_pass(self):
        async def unavailable(*args, **kw):
            raise httpx.ConnectError("unit unavailable")
        with patch.object(server.app.state.deps.quality_llm, "write_grounded", unavailable):
            result = await self.chat("payments-service 장애 진단")
        self.assertEqual(result["metadata"]["validation_attempts"], 1)
        self.assertEqual(result["metadata"]["validation_errors"], ["writer_unavailable"])
        self.assertEqual(result["metadata"]["validation_status"], "fallback")
        self.assertIn("검증 보류", result["reply"])

    async def test_general_diagnosis_collects_multiple_runbooks_and_skips_healthy_default(self):
        result = await self.chat("payments-service 장애 진단")
        runbooks = [e["target"] for e in result["events"] if e["type"] == "TaskCompleted" and e.get("source") == "runbook"]
        self.assertEqual(set(runbooks), {"cpu-spike", "memory-leak"})
        healthy = await self.chat("web-01 상태 진단", "healthy")
        self.assertFalse([e for e in healthy["events"] if e.get("source") == "runbook"])
        self.assertNotIn("DB Down", healthy["reply"])

    async def test_one_failed_service_does_not_discard_other_services_evidence(self):
        mcp = server.app.state.deps.mcp
        original = mcp.call_tool
        async def partly_failed(name, arguments):
            if arguments["system_name"] == "payments-service":
                raise ValueError("unit service failure")
            return await original(name, arguments)
        with patch.object(mcp, "call_tool", partly_failed):
            result = await self.chat("payments-service와 cache-service 장애 진단")
        self.assertIn("payments-service: 판단 보류", result["reply"])
        self.assertIn("cache-service: 상태", result["reply"])
        self.assertEqual(result["metadata"]["validation_status"], "partial")
        self.assertEqual(len(result["metadata"]["recovery_results"]), 1)
        self.assertEqual(result["metadata"]["recovery_results"][0]["snapshot"]["system_name"], "cache-service")

    async def test_incident_router_cannot_choose_an_unspecified_default_target(self):
        with patch.object(server.app.state.deps.mcp, "call_tool", side_effect=AssertionError("unexpected monitoring")):
            result = await self.chat("incident 티켓 처리해 주세요")
        self.assertIn("어떤 서비스", result["reply"])
        self.assertFalse(result["metadata"]["recovery_results"])
        resumed = await self.chat("payments-service")
        self.assertEqual(resumed["metadata"]["recovery_results"][0]["snapshot"]["system_name"], "payments-service")

    async def test_new_full_request_replaces_pending_report_request(self):
        await self.chat("보고서 내보내기")
        result = await self.chat("cache-service 장애 진단")
        self.assertEqual(result["metadata"]["recovery_action"], "legacy")
        self.assertEqual(result["metadata"]["validation_status"], "validated")
        self.assertFalse(result["metadata"]["reports"])

    async def test_data_expiring_during_generation_is_held_in_diagnosis_and_recovery(self):
        deps = server.app.state.deps
        original = deps.quality_llm.write_grounded
        now = deps.recovery_store.clock()
        async def expires(query, bundle, feedback=None):
            result = await original(query, bundle, feedback)
            deps.recovery_store.clock = lambda: now + timedelta(seconds=301)
            return result
        with patch.object(deps.quality_llm, "write_grounded", expires):
            result = await self.chat("payments-service 장애 진단")
        self.assertEqual(result["metadata"]["validation_status"], "fallback")
        self.assertNotIn("CPU 95%", result["reply"].split("## 상태 변화")[0])
        self.assertEqual(result["metadata"]["recovery_results"][0]["assessment"]["phase"], "hold")

    async def test_service_mentioned_in_thanks_does_not_create_an_observation(self):
        with patch.object(server.app.state.deps.mcp, "call_tool", side_effect=AssertionError("unexpected monitoring")):
            result = await self.chat("payments-service 도와주셔서 감사합니다")
        self.assertEqual(result["metadata"]["route"], "chat")
        self.assertFalse(server.app.state.deps.recovery_store.history("test-a", "payments-service"))

    async def test_null_model_response_is_held_instead_of_server_error(self):
        async def refused(*args, **kw):
            return None, {"usd": 0}
        with patch.object(server.app.state.deps.quality_llm, "write_grounded", refused):
            result = await self.chat("payments-service 장애 진단")
        self.assertEqual(result["metadata"]["validation_status"], "fallback")
        self.assertEqual(result["metadata"]["validation_attempts"], 2)

    async def test_invalid_monitoring_never_reaches_pattern_analysis(self):
        deps = server.app.state.deps
        original = deps.fast_llm.plan
        def needs_analysis(*args):
            result, usage = original(*args)
            result["needs_analysis"] = True
            return result, usage
        async def invalid(*args, **kw):
            return {"system_name": "payments-service", "status": "degraded", "cpu_percent": 999}
        with patch.object(deps.fast_llm, "plan", needs_analysis), patch.object(deps.mcp, "call_tool", invalid), patch.object(deps.fast_llm, "analyze", side_effect=AssertionError("invalid analysis input")):
            result = await self.chat("payments-service 장애 진단")
        self.assertEqual(result["metadata"]["validation_status"], "fallback")
        self.assertIn("판단 보류", result["reply"])
        self.assertNotIn("CPU 999%", result["reply"])

    async def test_route_and_plan_failure_use_confirmed_targets_without_fabricating_sources(self):
        deps = server.app.state.deps
        with patch.object(deps.fast_llm, "route", side_effect=httpx.ReadTimeout("unit route timeout")), patch.object(deps.fast_llm, "plan", side_effect=httpx.ReadTimeout("unit plan timeout")):
            result = await self.chat("payments-service 장애 진단")
        self.assertEqual(result["metadata"]["validation_status"], "validated")
        self.assertEqual([e["stage"] for e in result["events"] if e["type"] == "ModelFallback"], ["route", "plan"])
        self.assertEqual([r["snapshot"]["system_name"] for r in result["metadata"]["recovery_results"]], ["payments-service"])

    async def test_optional_analysis_failure_does_not_replace_collected_evidence(self):
        deps = server.app.state.deps
        original = deps.fast_llm.plan
        def needs_analysis(*args):
            result, usage = original(*args)
            result["needs_analysis"] = True
            return result, usage
        with patch.object(deps.fast_llm, "plan", needs_analysis), patch.object(deps.fast_llm, "analyze", side_effect=httpx.ReadTimeout("unit analysis timeout")):
            result = await self.chat("payments-service 장애 진단")
        self.assertEqual(result["metadata"]["validation_status"], "validated")
        self.assertTrue([e for e in result["events"] if e["type"] == "AnalysisSkipped" and "모델 분석 실패" in e["reason"]])

    async def test_failure_then_duplicate_stays_held_until_two_new_normal_observations(self):
        for _ in range(4):
            recovered = await self.chat("payments-service 복구 재점검")
        self.assertEqual(recovered["metadata"]["recovery_results"][0]["assessment"]["phase"], "recovered")
        deps = server.app.state.deps
        snapshot = recovered["metadata"]["recovery_results"][0]["snapshot"]
        with patch.object(deps.mcp, "call_tool", side_effect=ValueError("injected lookup failure")):
            self.assertIn("판단 보류", (await self.chat("다시 확인해줘"))["reply"])
        async def duplicate(*args, **kw):
            return snapshot
        with patch.object(deps.mcp, "call_tool", duplicate):
            replay = await self.chat("다시 확인해줘")
        self.assertEqual(replay["metadata"]["recovery_results"][0]["assessment"]["phase"], "hold")
        report = await self.chat("보고서 내보내기")
        self.assertIn("최근 재점검 실패", report["reply"])
        now = deps.recovery_store.clock()
        for index, expected in [(1, "partial"), (2, "recovered")]:
            moment = now + timedelta(seconds=index * 10)
            deps.recovery_store.clock = lambda: moment
            async def fresh(*args, **kw):
                return {**snapshot, "sample_id": f"fresh-after-failure-{index}", "last_checked": moment.isoformat()}
            with patch.object(deps.mcp, "call_tool", fresh):
                result = await self.chat("다시 확인해줘")
            self.assertEqual(result["metadata"]["recovery_results"][0]["assessment"]["phase"], expected)

    async def test_id_conflict_is_not_used_as_grounded_diagnosis_evidence(self):
        initial = await self.chat("payments-service 복구 재점검")
        snapshot = initial["metadata"]["recovery_results"][0]["snapshot"]
        async def conflict(*args, **kw):
            return {**snapshot, "cpu_percent": 12.345}
        with patch.object(server.app.state.deps.mcp, "call_tool", conflict):
            result = await self.chat("payments-service 장애 진단")
        self.assertEqual(result["metadata"]["validation_status"], "fallback")
        self.assertNotIn("CPU 12.345%", result["reply"])
        self.assertIn("동일 관측 ID", result["reply"])


if __name__ == "__main__":
    unittest.main()
