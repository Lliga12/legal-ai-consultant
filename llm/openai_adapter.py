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
