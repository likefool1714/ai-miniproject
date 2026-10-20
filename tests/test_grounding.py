import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from agent.grounding import (build_bundle, default_draft, fallback_draft, incident_types,
                             render_answer, validate_draft)
from agent.openai_llm import OpenAIQualityLLM
from agent.ollama_llm import OllamaQualityLLM, OllamaFastLLM
from agent.recovery import Observation, RecoveryPolicy


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
        self.policy = RecoveryPolicy()

    def payload(self, system="payments-service", **changes):
        return {"system_name": system, "status": "degraded", "cpu_percent": 89.5,
                "memory_percent": 55, "alerts": [], "last_checked": self.now.isoformat(),
                "sample_id": "fixed-1", "data_source": "test-fixture", **changes}

    def bundle(self, payloads=None, **kw):
        payloads = payloads or [self.payload()]
        collected = [{"source": "monitoring", "data": p} for p in payloads]
        for path in sorted((Path(__file__).resolve().parent.parent / "data/runbooks").glob("*.md")):
            collected.append({"source": "runbook", "target": path.stem, "data": path.read_text()})
        return build_bundle(collected, [p["system_name"] for p in payloads], self.policy, kw.get("now", self.now))

    def test_exact_observation_values_and_conditional_manual_are_separate(self):
        bundle = self.bundle()
        valid, errors = validate_draft(default_draft(bundle).model_dump_json(), bundle)
        self.assertFalse(errors)
        text = render_answer(bundle, valid, "validated")
        self.assertIn("CPU 89.5%", text)
        self.assertIn("fixed-1", text)
        self.assertIn("미실행", text)
        self.assertIn("조건 충족·지속 시간은 추가 확인", text)
        self.assertNotIn("90% 기준을 충족", text)
        self.assertIn("CPU 89.5% > 90% → 불충족", text)

    def test_fabricated_free_text_and_extra_numeric_fields_are_rejected(self):
        bundle = self.bundle()
        self.assertEqual(validate_draft("1) 로그를 분석했다. CPU 999%로 확정했다.", bundle)[1], ["schema_invalid"])
        raw = default_draft(bundle).model_dump()
        raw["conclusion"] = "최근 로그 분석에서 특이 패턴 없음"
        self.assertEqual(validate_draft(json.dumps(raw), bundle)[1], ["schema_invalid"])
        raw.pop("conclusion")
        raw["hypotheses"][0]["cpu_percent"] = 999
        self.assertEqual(validate_draft(json.dumps(raw), bundle)[1], ["schema_invalid"])

    def test_nonexistent_source_and_step_are_rejected(self):
        bundle = self.bundle()
        raw = {"hypotheses": [{"evidence_id": "M99", "kind": "cpu"}],
               "recommendations": [{"evidence_id": "M1", "step_id": "R99.1"}]}
        errors = validate_draft(json.dumps(raw), bundle)[1]
        self.assertIn("unsupported_hypothesis", errors)
        self.assertIn("unsupported_recommendation", errors)

    def test_service_specific_evidence_cannot_be_swapped(self):
        bundle = self.bundle([self.payload(), self.payload("cache-service", cpu_percent=40, memory_percent=89)])
        raw = default_draft(bundle).model_dump()
        raw["hypotheses"][1]["kind"] = "cpu"
        raw["recommendations"][1]["step_id"] = bundle["monitoring"][0]["allowed_step_ids"][0]
        errors = validate_draft(json.dumps(raw), bundle)[1]
        self.assertIn("unsupported_hypothesis", errors)
        self.assertIn("unsupported_recommendation", errors)

    def test_missing_service_and_duplicate_selection_fail_validation(self):
        bundle = self.bundle([self.payload(), self.payload("cache-service")])
        raw = default_draft(bundle).model_dump()
        raw["hypotheses"] = [raw["hypotheses"][0]] * 2
        errors = validate_draft(json.dumps(raw), bundle)[1]
        self.assertIn("missing_service_hypothesis", errors)
        self.assertIn("duplicate_hypothesis", errors)
        raw = default_draft(bundle).model_dump()
        raw["recommendations"] = []
        self.assertIn("missing_recommendation", validate_draft(json.dumps(raw), bundle)[1])

    def test_normal_service_does_not_get_db_down_or_other_incident_runbooks(self):
        bundle = self.bundle([self.payload("web-01", status="healthy", cpu_percent=34)])
        m = bundle["monitoring"][0]
        self.assertEqual(m["incident_types"], [])
        self.assertEqual(m["allowed_step_ids"], [])
        draft, errors = validate_draft(default_draft(bundle).model_dump_json(), bundle)
        self.assertFalse(errors)
        self.assertNotIn("DB Down", render_answer(bundle, draft, "validated"))

    def test_multiple_signals_fetch_all_matching_types_without_default_db(self):
        sample = Observation.from_payload(self.payload(memory_percent=90, alerts=["CPU high", "heap near limit"]))
        self.assertEqual(set(incident_types(sample, self.policy)), {"cpu-spike", "memory-leak"})
        down = Observation.from_payload(self.payload(status="down", cpu_percent=20))
        self.assertNotIn("db-down", incident_types(down, self.policy))

    def test_unvalidated_model_analysis_is_not_a_primary_source(self):
        collected = [{"source": "monitoring", "data": self.payload()},
                     {"source": "analysis", "data": "로그를 분석했다. CPU 999%."}]
        bundle = build_bundle(collected, ["payments-service"], self.policy, self.now)
        self.assertNotIn("999", json.dumps(bundle))
        self.assertNotIn("로그를 분석", render_answer(bundle, default_draft(bundle), "validated"))

    def test_stale_future_unknown_and_invalid_observations_never_become_zero_measurements(self):
        variants = [{"last_checked": (self.now - timedelta(seconds=301)).isoformat()},
                    {"last_checked": (self.now + timedelta(seconds=6)).isoformat()},
                    {"status": "unknown", "cpu_percent": 0}, {"cpu_percent": 999}]
        for changes in variants:
            with self.subTest(changes=changes):
                bundle = self.bundle([self.payload(**changes)])
                self.assertFalse(bundle["monitoring"])
                text = render_answer(bundle, fallback_draft(bundle), "fallback")
                self.assertIn("판단 보류", text)
                self.assertNotIn("CPU 0%", text)
                self.assertNotIn("CPU 999%", text)

    def test_evidence_ids_stay_stable_when_one_service_expires(self):
        bundle = self.bundle([self.payload(last_checked=(self.now - timedelta(seconds=301)).isoformat()),
                              self.payload("cache-service")])
        self.assertEqual(bundle["monitoring"][0]["id"], "M2")

    def test_fallback_has_no_model_hypothesis(self):
        bundle = self.bundle()
        draft = fallback_draft(bundle)
        self.assertFalse(draft.hypotheses)
        self.assertIn("검증 보류", render_answer(bundle, draft, "fallback"))


class APIWriterContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_model_has_bounded_non_thinking_generation(self):
        response = httpx.Response(200, json={"message": {"content": '{"route":"incident","needs_analysis":false}'}},
                                  request=httpx.Request("POST", "http://local.test"))
        with patch("agent.ollama_llm.httpx.post", return_value=response) as post:
            result, _ = OllamaFastLLM("qwen3:0.6b", 0.001).route("payments-service 장애 진단")
        self.assertEqual(result["route"], "incident")
        self.assertFalse(post.call_args.kwargs["json"]["think"])
        self.assertEqual(post.call_args.kwargs["json"]["options"]["num_predict"], 512)
        self.assertEqual(post.call_args.kwargs["timeout"], 20)

    async def test_proxy_preserves_timeout_and_invalid_backend_response_as_json(self):
        import web_demo_server
        original = httpx.AsyncClient
        async with original(transport=httpx.ASGITransport(app=web_demo_server.app), base_url="http://proxy") as client:
            for error, status in [(httpx.ReadTimeout("unit timeout"), 504), ("non-json", 502)]:
                async def upstream(request):
                    if isinstance(error, Exception):
                        raise error
                    return httpx.Response(500, text="backend unavailable")
                with patch("web_demo_server.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(upstream), **kw)):
                    response = await client.post("/chat", json={"message": "진단", "thread_id": "unit"})
                self.assertEqual(response.status_code, status)
                self.assertIn("보류", response.json()["detail"])

    async def test_ollama_schema_and_buffered_stream_contract(self):
        seen = []
        async def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, content='{"message":{"content":"{\\"hypotheses\\":[],\\"recommendations\\":[]}"},"done":true,"eval_count":5}\n')
        original = httpx.AsyncClient
        with patch("agent.ollama_llm.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw)):
            raw, usage = await OllamaQualityLLM("qwen3:8b", 0.002).write_grounded(
                "진단", {"monitoring": [], "runbooks": [], "unavailable": []})
        self.assertIsInstance(seen[0]["format"], dict)
        # 로컬 Ollama 문법 변환기는 이 정규식에서 \\d를 거부한다.
        self.assertNotIn("\\d", json.dumps(seen[0]["format"]))
        self.assertFalse(seen[0]["think"])
        self.assertEqual(json.loads(raw)["recommendations"], [])
        self.assertEqual(usage["tokens"], 5)

    async def test_api_json_mode_contract_without_real_network_or_key(self):
        from agent.grounding import writer_prompt
        bundle = {"monitoring": [], "runbooks": [], "unavailable": []}
        seen = []
        async def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"hypotheses":[],"recommendations":[]}'}}],
                                             "usage": {"total_tokens": 10}})
        original = httpx.AsyncClient
        with patch.dict("os.environ", {"OPENAI_API_KEY": "unit-test-placeholder"}):
            llm = OpenAIQualityLLM("gpt-4o", 0.01)
        with patch("agent.openai_llm.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw)):
            raw, usage = await llm.write_grounded("진단", bundle)
        self.assertEqual(seen[0]["response_format"], {"type": "json_object"})
        self.assertIn("JSON", writer_prompt("진단", bundle)[0])
        self.assertEqual(usage["tokens"], 10)
        self.assertEqual(json.loads(raw)["hypotheses"], [])
