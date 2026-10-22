"""외부 검토의 시간 경과·시연 시계·진단 의도 오분류 재현 검사."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import server
import test_recovery_api as fixtures
from agent.monitoring import MonitoringSource
from agent.recovery_nodes import command
from agent.recovery_store import RecoveryStore


class TimeWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 22, tzinfo=timezone.utc)
        self.start = self.now
        self.store = RecoveryStore(Path(self.temp.name) / "test.db", clock=lambda: self.now)
        self.count = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def record(self, second, bad=False):
        self.now = self.start + timedelta(seconds=second)
        self.count += 1
        return self.store.record("review", {
            "system_name": "payments-service", "status": "degraded" if bad else "healthy",
            "cpu_percent": 95 if bad else 55, "memory_percent": 60,
            "alerts": ["cpu"] if bad else [], "last_checked": self.now.isoformat(),
            "sample_id": str(self.count), "data_source": "review-clock-fixture"})["assessment"]

    def test_fresh_normals_keep_recovery_after_incident_is_five_minutes_old(self):
        self.record(0, True)
        for second in range(60, 601, 60):
            a = self.record(second)
            self.assertEqual(a["phase"], "partial" if second == 60 else "recovered")
        self.assertIn("복구 확인", self.store.report("review", "payments-service"))

    def test_long_incident_can_recover_after_three_hundred_seconds(self):
        self.record(0, True)
        self.record(600, True)
        self.assertEqual(self.record(660)["phase"], "partial")
        self.assertEqual(self.record(720)["phase"], "recovered")

    def test_stale_current_report_is_still_held(self):
        self.record(0, True)
        self.record(60)
        self.record(120)
        self.now += timedelta(seconds=301)
        self.assertIn("### payments-service: 판단 보류", self.store.report("review", "payments-service"))

    def test_normal_collection_gap_restarts_streak(self):
        self.record(0, True)
        self.record(60)
        a = self.record(361)
        self.assertEqual((a["phase"], a["normal_streak"]), ("partial", 1))
        self.assertEqual(self.record(421)["phase"], "recovered")

    def test_old_incident_outside_history_window_does_not_confirm_recovery(self):
        self.record(0, True)
        self.record(3601)
        self.assertEqual(self.record(3661)["phase"], "hold")

    def test_lookup_failure_still_breaks_streak_with_old_incident(self):
        self.record(0, True)
        self.record(300)
        self.store.failed("review", "payments-service", "fixture failure")
        a = self.record(360)
        self.assertEqual((a["phase"], a["normal_streak"]), ("partial", 1))
        self.assertEqual(self.record(420)["phase"], "recovered")

    def test_slow_scenario_reaches_recurrence_at_six_minutes(self):
        source = MonitoringSource(Path(__file__).resolve().parents[1] / "data",
                                  "recovery-demo", clock=lambda: self.now)
        actual = []
        for second in (0, 90, 180, 270, 360):
            self.now = self.start + timedelta(seconds=second)
            sample = source.get("payments-service", "slow")
            actual.append(self.store.record("slow", sample)["assessment"])
        self.assertEqual([a["phase"] for a in actual],
                         ["ongoing", "partial", "partial", "recovered", "ongoing"])
        self.assertTrue(actual[-1]["recurrence"])
        final = source.get("payments-service", "slow")
        self.now += timedelta(seconds=301)
        duplicate = source.get("payments-service", "slow")
        self.assertEqual(final, duplicate)
        self.assertEqual(self.store.record("slow", duplicate)["assessment"]["phase"], "hold")

    def test_scenario_contexts_advance_independently(self):
        source = MonitoringSource(Path(__file__).resolve().parents[1] / "data",
                                  "recovery-demo", clock=lambda: self.now)
        first = source.get("payments-service", "a")
        source.get("payments-service", "a")
        self.assertEqual(source.get("payments-service", "b")["cpu_percent"], first["cpu_percent"])


class IntentRegressionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RecoveryAPITests.asyncSetUp
    asyncTearDown = fixtures.RecoveryAPITests.asyncTearDown
    chat = fixtures.RecoveryAPITests.chat

    async def test_timeout_and_common_cause_queries_collect_every_requested_service(self):
        cases = [
            ("gateway-service에서 payments-service로 가는 요청이 타임아웃 나고 있어요", {"gateway-service", "payments-service"}),
            ("gateway-service에서 payments-service 호출 시 타임아웃이 발생합니다", {"gateway-service", "payments-service"}),
            ("payments-service와 cache-service 저하의 공통 원인을 분석해줘", {"payments-service", "cache-service"}),
        ]
        for i, (query, expected) in enumerate(cases):
            result = await self.chat(query, f"course-{i}")
            self.assertEqual(result["metadata"]["route"], "incident")
            self.assertEqual({r["snapshot"]["system_name"] for r in result["metadata"]["recovery_results"]}, expected)
            self.assertEqual(result["metadata"]["validation_status"], "validated")

    async def test_recovery_method_uses_diagnosis_and_writer(self):
        deps = server.app.state.deps
        with patch.object(deps.quality_llm, "write_grounded", wraps=deps.quality_llm.write_grounded) as writer:
            result = await self.chat("payments-service 장애 복구 방법 알려줘")
        self.assertEqual(result["metadata"]["recovery_action"], "legacy")
        self.assertEqual(result["metadata"]["route"], "incident")
        self.assertGreater(writer.call_count, 0)

    async def test_cause_and_log_followups_reuse_target_and_run_diagnosis(self):
        await self.chat("payments-service 복구 재점검")
        for query in ("회복이 안 돼요 원인 분석해줘", "로그를 다시 조사해서 원인을 찾아줘"):
            result = await self.chat(query)
            self.assertEqual(result["metadata"]["recovery_action"], "legacy")
            self.assertEqual(result["metadata"]["route"], "incident")
            self.assertEqual([r["snapshot"]["system_name"] for r in result["metadata"]["recovery_results"]], ["payments-service"])
            # 실제 로그 조회 도구가 없으므로 수집 완료를 주장해서는 안 된다.
            self.assertNotIn("로그를 분석했습니다", result["reply"])

    async def test_report_format_is_diagnosis_but_export_remains_stored_report(self):
        await self.chat("payments-service 복구 재점검")
        result = await self.chat("보고서 형식으로 정리해줘")
        self.assertEqual(result["metadata"]["recovery_action"], "legacy")
        self.assertEqual(result["metadata"]["route"], "incident")
        self.assertFalse(result["metadata"]["reports"])
        count = len(server.app.state.deps.recovery_store.history("test-a", "payments-service"))
        report = await self.chat("보고서 내보내기")
        self.assertEqual(report["metadata"]["recovery_action"], "report")
        self.assertTrue(report["metadata"]["reports"])
        self.assertEqual(len(server.app.state.deps.recovery_store.history("test-a", "payments-service")), count)

    async def test_unknown_target_never_replaced_with_default_service(self):
        with patch.object(server.app.state.deps.mcp, "call_tool", side_effect=AssertionError("unknown service queried")):
            result = await self.chat("billing-service 타임아웃 원인 분석해줘")
        self.assertIn("미등록 서비스", result["reply"])
        self.assertFalse(result["metadata"]["recovery_results"])


class CommandRegressionTests(unittest.TestCase):
    def test_diagnosis_and_status_and_format_are_distinct(self):
        for q in ("payments-service 장애 복구 방법 알려줘", "회복이 안 돼요 원인 분석해줘",
                  "로그를 다시 조사해서 원인을 찾아줘", "보고서 형식으로 정리해줘"):
            with self.subTest(query=q):
                self.assertEqual(command(q), "legacy")
        for q in ("복구됐어?", "다시 확인해줘", "복구 재점검"):
            self.assertEqual(command(q), "recheck")
        self.assertEqual(command("보고서 내보내기"), "report")
        self.assertEqual(command("조사 이력 보여줘"), "history")


if __name__ == "__main__":
    unittest.main()
