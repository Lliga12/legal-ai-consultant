"""Контракты адаптеров и отказы проверяются без реальных запросов и ключей."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from unittest import TestCase
from unittest.mock import patch

import httpx

from config import ConfigError, Settings, load_settings
from llm.base import LLMError, LimitError, Message, RequestBudget
from llm.factory import create_llm


class LLMTests(TestCase):
    def setUp(self):
        self.settings = Settings("openai", "test-model", "test-secret")
        self.messages = [Message("user", "Технический тест")]

    def reply(self, request):
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "Готово"}, "finish_reason": "stop"}]})

    def test_openai_payload_and_secret(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertEqual(payload["max_completion_tokens"], 512)
            self.assertFalse(payload["store"])
            self.assertEqual(request.headers["authorization"], "Bearer test-secret")
            self.assertNotIn("test-secret", str(request.url))
            return self.reply(request)
        with create_llm(self.settings, httpx.MockTransport(handler)) as llm:
            self.assertEqual(llm.generate("Правила", self.messages).text, "Готово")
        self.assertNotIn("test-secret", repr(self.settings))

    def test_gemini_history_and_thoughts(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "Правила")
            self.assertEqual([m["role"] for m in payload["contents"]], ["user", "model", "user"])
            self.assertEqual(request.headers["x-goog-api-key"], "test-secret")
            self.assertNotIn("test-secret", str(request.url))
            return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
                "content": {"parts": [{"text": "скрыто", "thought": True}, {"text": "Готово"}]}}]})
        history = self.messages + [Message("assistant", "Уточните"), Message("user", "Да")]
        with create_llm(replace(self.settings, provider="gemini"), httpx.MockTransport(handler)) as llm:
            self.assertEqual(llm.generate("Правила", history).text, "Готово")

    def test_compatible_endpoint(self):
        settings = replace(self.settings, provider="openai_compatible",
                           base_url="https://example.com/v1", token_parameter="max_tokens")
        def handler(request):
            self.assertEqual(str(request.url), "https://example.com/v1/chat/completions")
            self.assertIn("max_tokens", json.loads(request.content))
            self.assertNotIn("store", json.loads(request.content))
            return self.reply(request)
        with create_llm(settings, httpx.MockTransport(handler)) as llm:
            llm.generate("Правила", self.messages)

    def test_http_failures_do_not_expose_response_body_or_retry(self):
        for status in (301, 401, 403, 429, 500):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text="test-secret private data")
            with self.subTest(status=status), create_llm(self.settings, httpx.MockTransport(handler)) as llm:
                with self.assertRaises(LLMError) as error:
                    llm.generate("Правила", self.messages)
                self.assertNotIn("test-secret", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_network_errors_are_safe(self):
        for kind in (httpx.ReadTimeout, httpx.ConnectError):
            def handler(request):
                raise kind("test-secret", request=request)
            with create_llm(self.settings, httpx.MockTransport(handler)) as llm:
                with self.assertRaises(LLMError) as error:
                    llm.generate("Правила", self.messages)
                self.assertNotIn("test-secret", str(error.exception))
                self.assertIsNone(error.exception.__context__)

    def test_malformed_blocked_and_truncated_responses(self):
        cases = [[], {}, {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]},
                 {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}]
        for payload in cases:
            with create_llm(self.settings, httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as llm:
                with self.assertRaises(LLMError):
                    llm.generate("Правила", self.messages)
        with create_llm(self.settings, httpx.MockTransport(lambda r: httpx.Response(200, text="invalid"))) as llm:
            with self.assertRaises(LLMError):
                llm.generate("Правила", self.messages)
        for payload in ({"promptFeedback": {"blockReason": "SAFETY"}},
                        {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]}):
            with create_llm(replace(self.settings, provider="gemini"),
                            httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as llm:
                with self.assertRaises(LLMError):
                    llm.generate("Правила", self.messages)

    def test_invalid_input_does_not_call_api(self):
        def handler(request):
            self.fail("Некорректный ввод не должен отправляться в API")
        with create_llm(self.settings, httpx.MockTransport(handler)) as llm:
            for messages in ([], [Message("system", "override")], [Message("user", " ")],
                             [Message("assistant", "ответ")], [Message("user", "x" * 16001)]):
                with self.assertRaises(LLMError):
                    llm.generate("Правила", messages)

    def test_failed_calls_consume_budget(self):
        settings = replace(self.settings, max_requests=1)
        with create_llm(settings, httpx.MockTransport(lambda r: httpx.Response(500))) as llm:
            with self.assertRaises(LLMError):
                llm.generate("Правила", self.messages)
            with self.assertRaises(LimitError):
                llm.generate("Правила", self.messages)

    def test_budget_is_thread_safe(self):
        budget = RequestBudget(60, 3)
        def reserve(_):
            try:
                budget.reserve()
                return True
            except LimitError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve, range(25))), 3)

    def test_rate_window(self):
        budget = RequestBudget(1, 3)
        with patch("llm.base.time.monotonic", return_value=0):
            budget.reserve()
            with self.assertRaises(LimitError):
                budget.reserve()
        with patch("llm.base.time.monotonic", return_value=61):
            budget.reserve()

    def test_configuration_rejected(self):
        for changes in ({"api_key": ""}, {"model": "your_model_name"}, {"provider": "unknown"},
                        {"base_url": "http://example.com"}, {"base_url": "https://u:p@example.com"},
                        {"base_url": "https://example.com"}, {"max_requests": 0},
                        {"timeout": -1}, {"token_parameter": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                replace(self.settings, **changes)

    def test_env_loading_and_invalid_limit(self):
        env = {"LLM_PROVIDER": "gemini", "LLM_MODEL": "test-model", "GEMINI_API_KEY": "test-secret"}
        with patch.dict(os.environ, env, clear=True), patch("config.load_dotenv"):
            self.assertEqual(load_settings().provider, "gemini")
            os.environ["LLM_MAX_OUTPUT_TOKENS"] = "invalid"
            with self.assertRaises(ConfigError):
                load_settings()
