# Полный код этапа 3

Снимок исходных файлов для просмотра. Рабочие версии находятся по указанным путям.

## config.py

```python
"""Загрузка только серверной конфигурации; секреты не выводятся."""

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


class ConfigError(ValueError):
    pass


def positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ConfigError(f"{name}: требуется целое число.") from None
    if not 1 <= value <= maximum:
        raise ConfigError(f"{name}: допустимо от 1 до {maximum}.")
    return value


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str = "https://api.openai.com/v1"
    token_parameter: str = "max_completion_tokens"
    timeout: int = 30
    max_output_tokens: int = 512
    max_input_chars: int = 16000
    requests_per_minute: int = 5
    max_requests: int = 20

    def __post_init__(self) -> None:
        if self.provider not in {"gemini", "openai", "openai_compatible"}:
            raise ConfigError("LLM_PROVIDER: gemini, openai или openai_compatible.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", self.model):
            raise ConfigError("LLM_MODEL: задайте идентификатор модели из кабинета провайдера.")
        if self.model == "your_model_name":
            raise ConfigError("Замените LLM_MODEL на доступную вашему аккаунту модель.")
        if not self.api_key.strip() or any(c.isspace() for c in self.api_key):
            raise ConfigError("Не задан или некорректен API-ключ выбранного провайдера.")
        url = urlsplit(self.base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ConfigError("OPENAI_BASE_URL: требуется HTTPS URL без учётных данных и параметров.")
        if self.provider == "openai" and self.base_url != "https://api.openai.com/v1":
            raise ConfigError("Для другого API используйте LLM_PROVIDER=openai_compatible.")
        if self.token_parameter not in {"max_completion_tokens", "max_tokens"}:
            raise ConfigError("OPENAI_TOKEN_PARAMETER: max_completion_tokens или max_tokens.")
        for value, maximum in ((self.timeout, 120), (self.max_output_tokens, 4096),
                               (self.max_input_chars, 64000),
                               (self.requests_per_minute, 60), (self.max_requests, 1000)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ConfigError("Недопустимые ограничения запросов.")


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    key_name = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
                "openai_compatible": "OPENAI_COMPATIBLE_API_KEY"}.get(provider, "")
    return Settings(
        provider=provider,
        model=os.getenv("LLM_MODEL", "").strip(),
        api_key=os.getenv(key_name, "").strip() if key_name else "",
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
        token_parameter=os.getenv("OPENAI_TOKEN_PARAMETER", "max_completion_tokens").strip(),
        timeout=positive_int("LLM_TIMEOUT_SECONDS", 30, 120),
        max_output_tokens=positive_int("LLM_MAX_OUTPUT_TOKENS", 512, 4096),
        max_input_chars=positive_int("LLM_MAX_INPUT_CHARS", 16000, 64000),
        requests_per_minute=positive_int("LLM_REQUESTS_PER_MINUTE", 5, 60),
        max_requests=positive_int("LLM_MAX_REQUESTS_PER_PROCESS", 20, 1000),
    )
```

## llm/base.py

```python
"""Единый синхронный интерфейс; без зависимости от SDK провайдера."""

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from threading import Lock
import time
from typing import Sequence

import httpx

from config import Settings


class LLMError(RuntimeError):
    """Безопасная для вывода ошибка без тела ответа API и секретов."""


class LimitError(LLMError):
    pass


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str


class RequestBudget:
    """Общий бюджет объекта адаптера, потокобезопасный, до перезапуска процесса."""

    def __init__(self, per_minute: int, total: int):
        self.per_minute, self.total = per_minute, total
        self._times: deque[float] = deque()
        self._used = 0
        self._lock = Lock()

    def reserve(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._times and self._times[0] <= now - 60:
                self._times.popleft()
            if self._used >= self.total:
                raise LimitError("Исчерпан лимит запросов процесса.")
            if len(self._times) >= self.per_minute:
                raise LimitError("Слишком много запросов. Повторите позже.")
            self._used += 1
            self._times.append(now)


class BaseLLM(ABC):
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.budget = RequestBudget(settings.requests_per_minute, settings.max_requests)
        self._client = httpx.Client(timeout=settings.timeout, transport=transport,
                                    follow_redirects=False)

    def generate(self, system: str, messages: Sequence[Message]) -> LLMResponse:
        messages = tuple(messages)
        if not isinstance(system, str) or not system.strip() or not messages:
            raise LLMError("Нужны системная инструкция и сообщения.")
        for message in messages:
            if (not isinstance(message, Message) or message.role not in {"user", "assistant"}
                    or not isinstance(message.content, str) or not message.content.strip()):
                raise LLMError("Некорректная роль или пустое сообщение.")
        if messages[-1].role != "user":
            raise LLMError("Последнее сообщение должно быть от пользователя.")
        if len(system) + sum(len(m.content) for m in messages) > self.settings.max_input_chars:
            raise LimitError("Превышен допустимый размер контекста.")
        self.budget.reserve()
        return self._generate(system, messages)

    def post(self, url: str, headers: dict, payload: dict) -> dict:
        failed = False
        try:
            response = self._client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            reason = "Превышено время ожидания LLM. Повтор автоматически не выполнялся."
            failed = True
        except httpx.RequestError:
            reason = "Не удалось связаться с LLM. Проверьте сеть и настройки."
            failed = True
        if failed:
            raise LLMError(reason)
        if not 200 <= response.status_code < 300:
            raise LLMError(f"LLM API вернул HTTP {response.status_code}. Проверьте доступ и квоту.")
        try:
            data = response.json()
        except ValueError:
            raise LLMError("LLM API вернул некорректный JSON.") from None
        if not isinstance(data, dict):
            raise LLMError("Некорректная структура ответа LLM API.")
        return data

    @abstractmethod
    def _generate(self, system: str, messages: Sequence[Message]) -> LLMResponse:
        pass

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
```

## llm/gemini.py

```python
"""Gemini generateContent REST API; ключ передаётся только в заголовке."""

from typing import Sequence
from urllib.parse import quote

from llm.base import BaseLLM, LLMError, LLMResponse, Message


class GeminiAdapter(BaseLLM):
    def _generate(self, system: str, messages: Sequence[Message]) -> LLMResponse:
        model = quote(self.settings.model.removeprefix("models/"), safe="")
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "model" if m.role == "assistant" else "user",
                          "parts": [{"text": m.content}]} for m in messages],
            "generationConfig": {"maxOutputTokens": self.settings.max_output_tokens},
        }
        data = self.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": self.settings.api_key}, payload)
        try:
            candidate = data["candidates"][0]
            complete = candidate["finishReason"] == "STOP"
            parts = candidate["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("Gemini не вернул текст: возможна блокировка ответа.") from None
        if not complete or not text.strip():
            raise LLMError("Gemini не вернул завершённый текст: блокировка или лимит токенов.")
        return LLMResponse(text.strip(), self.settings.provider, self.settings.model)
```

## llm/openai_adapter.py

```python
"""OpenAI Chat Completions и явно выбранные совместимые сервисы."""

from typing import Sequence

from llm.base import BaseLLM, LLMError, LLMResponse, Message


class OpenAIAdapter(BaseLLM):
    def _generate(self, system: str, messages: Sequence[Message]) -> LLMResponse:
        payload = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            self.settings.token_parameter: self.settings.max_output_tokens,
        }
        if self.settings.provider == "openai":
            payload["store"] = False
        data = self.post(self.settings.base_url + "/chat/completions",
                         {"Authorization": "Bearer " + self.settings.api_key}, payload)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            complete = choice["finish_reason"] == "stop"
        except (KeyError, IndexError, TypeError):
            raise LLMError("Неполный ответ OpenAI API.") from None
        if not complete or not isinstance(text, str) or not text.strip():
            raise LLMError("Модель не вернула завершённый текст: блокировка или лимит токенов.")
        return LLMResponse(text.strip(), self.settings.provider, self.settings.model)
```

## llm/factory.py

```python
"""Основной код зависит от BaseLLM, а не от конкретного провайдера."""

import httpx

from config import Settings, load_settings
from llm.base import BaseLLM
from llm.gemini import GeminiAdapter
from llm.openai_adapter import OpenAIAdapter


def create_llm(settings: Settings | None = None,
               transport: httpx.BaseTransport | None = None) -> BaseLLM:
    settings = settings or load_settings()
    adapter = GeminiAdapter if settings.provider == "gemini" else OpenAIAdapter
    return adapter(settings, transport)
```

## app.py

```python
"""Проверка структуры и явный тест облачной модели (этапы 1–3)."""

import argparse
from pathlib import Path
import sys


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Требуется Python 3.10 или новее.")
    parser = argparse.ArgumentParser(description="AI-консультант юриста: этап 3")
    parser.add_argument("--check-llm", action="store_true", help="Один реальный тестовый запрос к LLM API")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    required = ("knowledge", "core", "llm", "tools", "database", "ui", "tests")
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise SystemExit("Отсутствуют каталоги: " + ", ".join(missing))
    if not args.check_llm:
        print("AI-консультант юриста — структура проекта готова.")
        print("Этап 3. Для проверки LLM: python app.py --check-llm. Веб-чат ещё не реализован.")
        return
    try:
        from config import ConfigError, load_settings
        from llm.base import LLMError, Message
        from llm.factory import create_llm
    except ImportError:
        raise SystemExit("Установите зависимости: python -m pip install -r requirements.txt") from None
    try:
        settings = load_settings()
        with create_llm(settings) as llm:
            response = llm.generate(
                "Это техническая проверка соединения. Ответь одним коротким словом по-русски.",
                [Message("user", "Ответь: готово.")])
        print(f"LLM API ответил: {response.provider}, модель {response.model}")
        print(response.text)
    except (ConfigError, LLMError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
```

## .env.example

```text
# Скопируйте в .env. Заполняйте ключ локально, не отправляйте в чат.
LLM_PROVIDER=gemini
LLM_MODEL=your_model_name
GEMINI_API_KEY=
OPENAI_API_KEY=
# Для другого сервиса: LLM_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_TOKEN_PARAMETER=max_completion_tokens
# Для совместимого API при необходимости: OPENAI_TOKEN_PARAMETER=max_tokens
LLM_TIMEOUT_SECONDS=30
LLM_MAX_OUTPUT_TOKENS=512
LLM_MAX_INPUT_CHARS=16000
LLM_REQUESTS_PER_MINUTE=5
LLM_MAX_REQUESTS_PER_PROCESS=20
# Будущие этапы; на этапе 3 не используются.
SUPABASE_URL=
SUPABASE_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## requirements.txt

```text
httpx==0.28.1
python-dotenv==1.2.1
pytest==8.4.2
```

## tests/test_llm.py

```python
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
```
