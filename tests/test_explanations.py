"""상태 해석·미실행 점검 방향이 근거와 복구 판정에 모순되지 않는지 검증한다."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.grounding import (Recommendation, build_bundle, default_draft,
                             render_answer, validate_draft, writer_schema)
from agent.recovery import RecoveryPolicy, assess, render_assessment


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        self.policy = RecoveryPolicy()
        self.root = Path(__file__).resolve().parent.parent

    def sample(self, **changes):
        return {"system_name": "payments-service", "status": "degraded", "cpu_percent": 89.5,
                "memory_percent": 55, "alerts": ["CPU high"], "sample_id": "current",
                "last_checked": self.now.isoformat(), **changes}

    def bundle(self, sample=None, books=("cpu-spike",), policy=None):
        collected = [{"source": "monitoring", "data": sample or self.sample()}]
        collected += [{"source": "runbook", "target": name,
                       "data": (self.root / "data/runbooks" / f"{name}.md").read_text()} for name in books]
        return build_bundle(collected, ["payments-service"], policy or self.policy, self.now)

    def test_cpu_interpretation_connects_evidence_to_checks_without_claiming_cause(self):
        bundle = self.bundle()
        text = render_answer(bundle, default_draft(bundle), "validated")
        self.assertLess(text.index("## 진단 요약"), text.index("## 확인된 모니터링 근거"))
        self.assertIn("CPU 부하를 우선 점검할 근거", text)
        self.assertIn("메모리 55%는 정상 기준 안", text)
        self.assertIn("반복 호출, 배치 작업 중복", text)
        self.assertIn("원인으로 단정하지 않습니다", text)
        self.assertNotIn("로그를 분석했다", text)

    def test_active_cpu_alert_is_not_mistaken_for_current_high_cpu(self):
        bundle = self.bundle(self.sample(cpu_percent=50))
        text = render_answer(bundle, default_draft(bundle), "validated")
        self.assertIn("CPU 50%와 메모리 55%는 설정한 정상 기준 안", text)
        self.assertIn("알림 내용과 실제 증상을 대조", text)
        self.assertNotIn("CPU 부하를 우선 점검할 근거", text)

    def test_generation_caps_duplicates_without_removing_multiple_supported_signals(self):
        cpu_only = writer_schema(self.bundle())
        self.assertEqual(cpu_only["properties"]["hypotheses"]["maxItems"], 1)
        self.assertEqual(cpu_only["properties"]["recommendations"]["maxItems"], 3)
        both = writer_schema(self.bundle(self.sample(memory_percent=90), books=("cpu-spike", "memory-leak")))
        self.assertEqual(both["properties"]["hypotheses"]["maxItems"], 2)
        healthy = writer_schema(self.bundle(self.sample(status="healthy", cpu_percent=50, alerts=[])))
        self.assertEqual(healthy["properties"]["recommendations"]["maxItems"], 0)

    def test_interpretation_uses_configured_policy_not_hardcoded_eighty(self):
        bundle = self.bundle(policy=RecoveryPolicy(cpu_limit=95))
        text = render_answer(bundle, default_draft(bundle), "validated")
        self.assertIn("CPU 89.5%와 메모리 55%는 설정한 정상 기준 안", text)
        self.assertNotIn("CPU 부하를 우선 점검할 근거", text)

    def test_specific_manual_explanation_requires_unchanged_retrieved_runbook(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                bundle = self.bundle(books=("cpu-spike",) if changed else ())
                if changed:
                    bundle["runbooks"][0]["steps"][1]["text"] = "요청 분포를 확인한다."
                text = render_answer(bundle, default_draft(bundle), "validated")
                self.assertNotIn("요청·트래픽 변화나 반복 호출", text)

    def test_selected_scaling_step_is_deferred_at_or_below_strict_threshold(self):
        for cpu, met in ((89.5, False), (90, False), (90.1, True)):
            with self.subTest(cpu=cpu):
                bundle = self.bundle(self.sample(cpu_percent=cpu))
                draft = default_draft(bundle)
                draft.recommendations = [Recommendation(evidence_id="M1", step_id="R1.3")]
                valid, errors = validate_draft(draft.model_dump_json(), bundle)
                self.assertFalse(errors)
                text = render_answer(bundle, valid, "validated")
                self.assertIn("지속 시간·기타 조치 조건은 미확인", text)
                if met:
                    self.assertIn("고부하가 지속되는지와 증설 영향을 확인한 뒤", text)
                else:
                    self.assertIn("현재 수치 조건 미충족으로 보류할 조치", text)
                    self.assertIn("먼저 요청·작업 패턴을 점검", text)

    def recovery(self, samples):
        result = assess([{"snapshot": s} for s in samples], self.policy, self.now)
        return result, render_assessment("payments-service", samples[-1], result)

    def test_partial_with_remaining_memory_problem_explains_what_to_check(self):
        previous = self.sample(cpu_percent=95, memory_percent=90, alerts=["CPU high", "memory high"],
                               last_checked=(self.now-timedelta(seconds=10)).isoformat(), sample_id="previous")
        result, text = self.recovery([previous, self.sample(cpu_percent=65, memory_percent=87, alerts=["memory high"])])
        self.assertEqual(result["phase"], "partial")
        self.assertIn("이전보다 지표가 개선됐지만", text)
        self.assertIn("메모리 사용 추세", text)
        self.assertIn("남아 있는 알림의 원인과 해소 여부", text)

    def test_one_normal_explains_why_and_when_to_recheck(self):
        previous = self.sample(last_checked=(self.now-timedelta(seconds=10)).isoformat(), sample_id="previous")
        result, text = self.recovery([previous, self.sample(status="healthy", cpu_percent=50, alerts=[])])
        self.assertEqual(result["phase"], "partial")
        self.assertIn("일시적으로만 정상화됐을 가능성", text)
        self.assertIn("1/2회", text)
        self.assertIn("최소 5초 이상 떨어진 새 자료", text)

    def test_held_old_normal_sample_is_not_explained_as_current_normalization(self):
        _, text = self.recovery([self.sample(status="healthy", cpu_percent=50, alerts=[],
                                            last_checked=(self.now-timedelta(seconds=301)).isoformat())])
        self.assertIn("판단 보류", text)
        self.assertNotIn("현재 수치와 알림은 정상 기준으로 돌아왔습니다", text)

    def test_recovered_means_policy_verdict_and_not_successful_user_requests(self):
        samples = [self.sample(last_checked=(self.now-timedelta(seconds=20)).isoformat(), sample_id="bad"),
                   self.sample(status="healthy", cpu_percent=50, alerts=[], last_checked=(self.now-timedelta(seconds=10)).isoformat(), sample_id="normal-1"),
                   self.sample(status="healthy", cpu_percent=50, alerts=[])]
        result, text = self.recovery(samples)
        self.assertEqual(result["phase"], "recovered")
        self.assertIn("정책상 복구", text)
        self.assertIn("실제 사용자 요청 성공까지 검증한 것은 아닙니다", text)
