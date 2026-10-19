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
