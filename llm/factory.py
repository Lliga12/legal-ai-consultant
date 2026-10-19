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
