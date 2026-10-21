"""추가 감사에서 발견한 결함과 수정 시 지켜야 할 예외 경계를 검사한다."""
import asyncio
import unittest
from datetime import timedelta
from unittest.mock import patch

from pydantic import ValidationError

import server
import test_recovery_api as fixtures
from agent.recovery import Observation, utc_now
from agent.recovery_nodes import extract_targets


class AuditRegressionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RecoveryAPITests.asyncSetUp
    asyncTearDown = fixtures.RecoveryAPITests.asyncTearDown
    chat = fixtures.RecoveryAPITests.chat

    def seed(self, thread):
        store = server.app.state.deps.recovery_store
        now = utc_now()
        def payload(i, bad=False):
            return {"system_name": "payments-service", "status": "degraded" if bad else "healthy",
                    "cpu_percent": 95 if bad else 55, "memory_percent": 60, "alerts": [],
                    "last_checked": (now - timedelta(seconds=30-i*10)).isoformat(), "sample_id": str(i)}
        store.record(thread, payload(0, True))
        store.record(thread, payload(1))
        store.set_context(thread, targets=["payments-service"], pending=None)
        return store, payload

    async def test_request_timeout_breaks_streak_for_post_and_sse(self):
        for mode in ("post", "sse"):
            with self.subTest(mode=mode):
                thread = "outer-timeout-" + mode
                store, payload = self.seed(thread)
                async def slow(*args, **kwargs):
                    await asyncio.sleep(2)
                with patch.object(server.app.state.deps.mcp, "call_tool", slow), patch.object(server, "REQUEST_TIMEOUT_SECONDS", 0.03):
                    if mode == "post":
                        response = await self.client.post("/chat", json={"message": "다시 확인해줘", "thread_id": thread})
                        self.assertEqual(response.status_code, 504)
                    else:
                        response = await self.client.get("/chat/stream", params={"message": "다시 확인해줘", "thread_id": thread})
                        self.assertIn('"type": "Error"', response.text)
                self.assertIn("최근 재점검 실패", store.report(thread, "payments-service"))
                async def fresh(*args, **kwargs):
                    return payload(2)
                with patch.object(server.app.state.deps.mcp, "call_tool", fresh):
                    result = await self.chat("다시 확인해줘", thread)
                a = result["metadata"]["recovery_results"][0]["assessment"]
                self.assertEqual((a["phase"], a["normal_streak"]), ("partial", 1))

    async def test_timeout_waiting_for_thread_lock_does_not_invalidate_previous_observation(self):
        thread = "waiting-only"
        store, _ = self.seed(thread)
        async with server._thread_lock(thread):
            with patch.object(server, "REQUEST_TIMEOUT_SECONDS", 0.02), patch.object(
                    server.app.state.deps.mcp, "call_tool", side_effect=AssertionError("collection not started")):
                response = await self.client.post("/chat", json={"message": "다시 확인해줘", "thread_id": thread})
                self.assertEqual(response.status_code, 504)
        failure = store.conn.execute("SELECT * FROM recovery_failures WHERE thread_id=?", (thread,)).fetchone()
        self.assertIsNone(failure)

    async def test_cancelling_actual_collection_breaks_streak_in_both_routes(self):
        for message in ("다시 확인해줘", "payments-service 장애 진단"):
            with self.subTest(message=message):
                thread = "cancel-" + str(len(message))
                store, _ = self.seed(thread)
                entered = asyncio.Event()
                async def slow(*args, **kwargs):
                    entered.set()
                    await asyncio.sleep(30)
                with patch.object(server.app.state.deps.mcp, "call_tool", slow):
                    task = asyncio.create_task(self.client.post("/chat", json={"message": message, "thread_id": thread}))
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertIn("최근 재점검 실패", store.report(thread, "payments-service"))

    async def test_bool_metrics_are_held_at_api_without_incrementing_normal_streak(self):
        store, payload = self.seed("bool-api")
        async def invalid(*args, **kwargs):
            return {**payload(2), "cpu_percent": True, "memory_percent": False}
        with patch.object(server.app.state.deps.mcp, "call_tool", invalid):
            result = await self.chat("다시 확인해줘", "bool-api")
        self.assertIn("판단 보류", result["reply"])
        self.assertEqual(result["metadata"]["recovery_results"], [])
        self.assertEqual(len(store.history("bool-api", "payments-service")), 2)
        self.assertIn("최근 재점검 실패", store.report("bool-api", "payments-service"))

    async def test_excluded_target_is_not_queried(self):
        result = await self.chat("결제 서비스 말고 인증 서비스만 다시 확인해 주세요")
        targets = [r["snapshot"]["system_name"] for r in result["metadata"]["recovery_results"]]
        self.assertEqual(targets, ["auth-service"])
        self.assertEqual(server.app.state.deps.recovery_store.history("test-a", "payments-service"), [])

    async def test_excluding_only_target_does_not_restore_it_from_context(self):
        await self.chat("payments-service 복구 재점검")
        with patch.object(server.app.state.deps.mcp, "call_tool", side_effect=AssertionError("excluded target reused")):
            result = await self.chat("결제 서비스는 제외하고 다시 확인해 주세요")
        self.assertIn("어떤 서비스", result["reply"])
        self.assertEqual(len(server.app.state.deps.recovery_store.history("test-a", "payments-service")), 1)

    async def test_short_target_answer_keeps_pending_recheck(self):
        await self.chat("payments-service와 cache-service 복구 재점검")
        self.assertIn("어떤 서비스", (await self.chat("그 서비스 복구됐어?"))["reply"])
        with patch.object(server.app.state.deps.fast_llm, "route", side_effect=AssertionError("unnecessary route model")), patch.object(
                server.app.state.deps.quality_llm, "write_grounded", side_effect=AssertionError("unnecessary writer")):
            result = await self.chat("결제 서비스 상태요")
        self.assertEqual(result["metadata"]["recovery_action"], "recheck")
        self.assertEqual([r["snapshot"]["system_name"] for r in result["metadata"]["recovery_results"]], ["payments-service"])

    async def test_short_target_answer_can_resume_pending_report(self):
        await self.chat("payments-service 복구 재점검")
        await self.chat("보고서 내보내기", "empty-context")
        # 확인 대기 중인 다른 대화에 기록만 옮기지 않는다. '이력 없음'도 원래 report 경로여야 한다.
        result = await self.chat("결제 서비스 상태요", "empty-context")
        self.assertEqual(result["metadata"]["recovery_action"], "report")
        self.assertIn("조사 기록이 없어", result["reply"])


class TargetAndSchemaTests(unittest.TestCase):
    def test_exclusion_forms_and_mention_order(self):
        cases = [
            ("결제 서비스 말고 인증 서비스만 재점검", ["auth-service"], []),
            ("payments-service 제외하고 auth-service 재점검", ["auth-service"], []),
            ("결제 서비스 빼고 인증서비스 재점검", ["auth-service"], []),
            ("결제 서비스와 캐시 서비스는 제외하고 인증 서비스 재점검", ["auth-service"], []),
            ("billing-service 말고 인증 서비스 재점검", ["auth-service"], []),
            ("인증 서비스와 payments-service 재점검", ["auth-service", "payments-service"], []),
            ("payments-service와 auth-service 모두 재점검", ["payments-service", "auth-service"], []),
            ("payments-service 제외하지 말고 auth-service도 재점검", ["payments-service", "auth-service"], []),
        ]
        for query, known, unknown in cases:
            with self.subTest(query=query):
                self.assertEqual(extract_targets(query), (known, unknown))

    def test_observation_and_mcp_schema_require_real_json_numbers(self):
        from mcp_server import SystemStatus
        base = {"system_name": "payments-service", "status": "healthy", "cpu_percent": 55,
                "memory_percent": 60, "alerts": [], "sample_id": "schema-test",
                "last_checked": utc_now().isoformat(), "data_source": "regression-fixture"}
        for model in (Observation, SystemStatus):
            for field in ("cpu_percent", "memory_percent"):
                for invalid in (True, False, "55"):
                    with self.subTest(model=model.__name__, field=field, invalid=invalid):
                        with self.assertRaises(ValidationError):
                            model.model_validate({**base, field: invalid})
                for valid in (0, 55.5, 100):
                    model.model_validate({**base, field: valid})
