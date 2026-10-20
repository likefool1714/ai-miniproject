import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from agent.monitoring import MonitoringSource
from agent.recovery import Observation, RecoveryPolicy
from agent.recovery_store import RecoveryStore


class RecoveryTests(unittest.TestCase):
    def test_failed_lookup_breaks_normal_streak_and_survives_restart(self):
        self.record(status="degraded", cpu=95)
        self.record()
        self.store.failed("thread-a", "payments-service", "connection failed")
        self.store.close()
        self.store = RecoveryStore(self.path, self.policy, clock=lambda: self.now)
        first = self.record()["assessment"]
        self.assertEqual(first["phase"], "partial")
        self.assertEqual(first["normal_streak"], 1)
        self.assertEqual(self.record()["assessment"]["phase"], "recovered")

    def test_duplicate_after_failed_recheck_cannot_clear_failure(self):
        self.record(status="degraded", cpu=95)
        self.record()
        recovered = self.record()
        self.store.failed("thread-a", "payments-service", "connection failed")
        duplicate = self.store.record("thread-a", recovered["snapshot"])
        self.assertEqual(duplicate["assessment"]["phase"], "hold")
        self.assertFalse(duplicate["usable"])
        self.assertIn("최근 재점검 실패", self.store.report("thread-a", "payments-service"))
        self.assertEqual(self.record()["assessment"]["phase"], "partial")
        self.assertEqual(self.record()["assessment"]["phase"], "recovered")

    def test_replaying_original_after_id_conflict_does_not_resolve_conflict(self):
        self.record(status="degraded", cpu=95)
        self.record()
        recovered = self.record()
        self.store.record("thread-a", {**recovered["snapshot"], "cpu_percent": 99})
        original = self.store.record("thread-a", recovered["snapshot"])
        self.assertEqual(original["assessment"]["phase"], "hold")
        self.assertIn("판단 보류", self.store.report("thread-a", "payments-service"))

    def test_older_known_id_does_not_masquerade_as_a_current_success(self):
        bad = self.record(status="degraded", cpu=95)
        self.record()
        self.record()
        replay = self.store.record("thread-a", bad["snapshot"])
        self.assertEqual(replay["assessment"]["phase"], "hold")
        self.assertFalse(replay["usable"])
        self.assertIn("과거 관측 ID", " ".join(replay["assessment"]["reasons"]))

    def test_report_uses_same_500_record_verdict_window_as_live_assessment(self):
        self.record(status="degraded", cpu=95)
        for _ in range(130):
            self.now += timedelta(seconds=1)
            current = self.store.record("thread-a", self.payload())
        self.assertEqual(current["assessment"]["phase"], "recovered")
        report = self.store.report("thread-a", "payments-service")
        self.assertIn("### payments-service: 복구 확인", report)
        self.assertIn("현재 100건", report)
        # 저장 직전 500건에 새 관측을 더해 501건을 판정하는 실수도 방지한다.
        for _ in range(371):
            self.now += timedelta(seconds=0.1)
            current = self.store.record("thread-a", self.payload())
        self.assertEqual(current["assessment"]["phase"], "hold")
        self.assertIn("### payments-service: 판단 보류", self.store.report("thread-a", "payments-service"))

    def test_invalid_future_sample_does_not_poison_subsequent_new_observations(self):
        self.record(status="degraded", cpu=95)
        invalid = self.store.record("thread-a", self.payload(last_checked=(self.now + timedelta(days=1)).isoformat()))
        self.assertEqual(invalid["assessment"]["phase"], "hold")
        self.assertEqual(self.record()["assessment"]["phase"], "partial")
        self.assertEqual(self.record()["assessment"]["phase"], "recovered")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
        self.policy = RecoveryPolicy()
        self.path = Path(self.temp.name) / "history.db"
        self.store = RecoveryStore(self.path, self.policy, clock=lambda: self.now)
        self.counter = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def payload(self, status="healthy", cpu=50, memory=50, alerts=None, **overrides):
        self.counter += 1
        return {"system_name": "payments-service", "status": status, "cpu_percent": cpu,
                "memory_percent": memory, "alerts": alerts or [], "last_checked": self.now.isoformat(),
                "sample_id": str(self.counter), "data_source": "unit-fixture", **overrides}

    def record(self, **kw):
        self.now += timedelta(seconds=10)
        return self.store.record("thread-a", self.payload(**kw))

    def test_recovery_requires_all_signals_and_two_separated_normal_samples(self):
        self.assertEqual(self.record(status="degraded", cpu=95, memory=90, alerts=["CPU high", "memory high"])["assessment"]["phase"], "ongoing")
        partial = self.record(status="degraded", cpu=65, memory=87, alerts=["memory high"])
        self.assertEqual(partial["assessment"]["phase"], "partial")
        self.assertEqual(partial["assessment"]["comparison"]["cpu_delta_pp"], -30)
        self.assertEqual(partial["assessment"]["comparison"]["alerts_cleared"], ["CPU high"])
        self.assertEqual(self.record()["assessment"]["phase"], "partial")
        recovered = self.record()
        self.assertEqual(recovered["assessment"]["phase"], "recovered")
        self.assertEqual(recovered["assessment"]["normal_streak"], 2)
        recurrence = self.record(status="degraded", cpu=95, alerts=["CPU high again"])
        self.assertTrue(recurrence["assessment"]["recurrence"])

    def test_temporary_normal_then_bad_is_not_recurrence_without_confirmed_recovery(self):
        self.record(status="degraded", cpu=95)
        self.assertEqual(self.record()["assessment"]["phase"], "partial")
        bad = self.record(status="degraded", cpu=95)
        self.assertFalse(bad["assessment"]["recurrence"])
        self.assertEqual(bad["assessment"]["phase"], "ongoing")

    def test_changed_policy_is_not_misreported_as_recurrence(self):
        self.record(status="degraded", cpu=95)
        self.record()
        self.record()
        self.store.policy = RecoveryPolicy(cpu_limit=40)
        result = self.record()
        self.assertFalse(result["assessment"]["recurrence"])

    def test_normal_without_incident_cannot_claim_recovery(self):
        self.record()
        self.assertEqual(self.record()["assessment"]["phase"], "hold")

    def test_alerts_memory_and_exact_threshold_prevent_recovery(self):
        self.record(status="degraded", cpu=95)
        for kw in ({"memory": 85}, {"cpu": 80}, {"alerts": ["latency high"]}):
            result = self.record(**kw)
            self.assertNotEqual(result["assessment"]["phase"], "recovered")

    def test_duplicate_observation_does_not_increment_or_refresh_time(self):
        self.record(status="degraded", cpu=95)
        self.now += timedelta(seconds=10)
        sample = self.payload()
        first = self.store.record("thread-a", sample)
        self.now += timedelta(seconds=10)
        duplicate = self.store.record("thread-a", sample)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(duplicate["assessment"]["normal_streak"], 1)
        self.assertEqual(len(self.store.history("thread-a", "payments-service")), 2)
        self.now += timedelta(seconds=301)
        self.assertEqual(self.store.record("thread-a", sample)["assessment"]["phase"], "hold")

    def test_too_close_normals_and_out_of_order_samples_do_not_confirm(self):
        self.record(status="degraded", cpu=95)
        self.record()
        self.now += timedelta(seconds=1)
        self.assertEqual(self.store.record("thread-a", self.payload())["assessment"]["phase"], "partial")
        old = self.payload(last_checked=(self.now - timedelta(seconds=2)).isoformat())
        self.assertEqual(self.store.record("thread-a", old)["assessment"]["phase"], "hold")

    def test_conflicting_duplicate_and_failed_recheck_block_report_recovery(self):
        self.record(status="degraded", cpu=95)
        self.record()
        recovered = self.record()
        changed = {**recovered["snapshot"], "cpu_percent": 99}
        self.assertEqual(self.store.record("thread-a", changed)["assessment"]["phase"], "hold")
        self.assertIn("### payments-service: 판단 보류", self.store.report("thread-a", "payments-service"))
        self.record()
        self.store.failed("thread-a", "payments-service", "조회 시간 초과")
        self.assertIn("최근 재점검 실패", self.store.report("thread-a", "payments-service"))

    def test_stale_future_unknown_and_invalid_data(self):
        for kw in ({"last_checked": (self.now - timedelta(seconds=301)).isoformat()},
                   {"last_checked": (self.now + timedelta(seconds=30)).isoformat()},
                   {"status": "unknown", "cpu": 0, "memory": 0}):
            result = self.store.record("thread-a", self.payload(**kw))
            self.assertEqual(result["assessment"]["phase"], "hold")
        for cpu in (101, -1, float("nan"), float("inf")):
            with self.assertRaises(ValidationError):
                Observation.from_payload(self.payload(cpu=cpu))

    def test_stale_previous_snapshot_does_not_produce_delta_or_recovery(self):
        self.record(status="degraded", cpu=95)
        self.now += timedelta(seconds=301)
        current = self.record()
        self.assertIsNone(current["assessment"]["comparison"])
        self.assertEqual(current["assessment"]["phase"], "hold")

    def test_thread_service_isolation_and_restart_persistence(self):
        self.store.set_context("thread-a", targets=["payments-service"])
        self.record(status="degraded", cpu=95)
        self.store.record("thread-a", self.payload(system_name="auth-service"))
        self.assertEqual(self.store.history("thread-b", "payments-service"), [])
        self.assertEqual(len(self.store.history("thread-a", "auth-service")), 1)
        self.store.close()
        self.store = RecoveryStore(self.path, clock=lambda: self.now)
        self.assertEqual(self.store.context("thread-a")["targets"], ["payments-service"])
        self.assertEqual(len(self.store.history("thread-a", "payments-service")), 1)

    def test_report_reassesses_freshness_without_new_observation(self):
        self.record(status="degraded", cpu=95)
        self.record()
        self.record()
        report = self.store.report("thread-a", "payments-service")
        self.assertIn("복구 확인", report)
        self.now += timedelta(seconds=301)
        report = self.store.report("thread-a", "payments-service")
        self.assertIn("### payments-service: 판단 보류", report)
        self.assertEqual(len(self.store.history("thread-a", "payments-service")), 3)
        self.assertIsNone(self.store.report("thread-b", "payments-service"))

    def test_scenario_repeats_final_observation_with_same_identifier(self):
        source = MonitoringSource(Path(__file__).resolve().parent.parent / "data", "recovery-demo", clock=lambda: self.now)
        samples = [source.get("payments-service") for _ in range(6)]
        self.assertEqual(samples[-1]["sample_id"], samples[-2]["sample_id"])
        self.assertEqual(len(set(s["sample_id"] for s in samples)), 5)
        for s in samples:
            Observation.from_payload(s)
            self.assertLessEqual(datetime.fromisoformat(s["last_checked"]), self.now)
        new_thread_first = source.get("payments-service", "new-thread")
        self.assertEqual(new_thread_first["cpu_percent"], 95)
        self.assertNotEqual(new_thread_first["sample_id"], samples[-1]["sample_id"])


if __name__ == "__main__":
    unittest.main()
