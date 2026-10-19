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
