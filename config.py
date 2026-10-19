"""Загрузка только серверной конфигурации; секреты не выводятся."""

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
from urllib.parse import urlsplit
import base64
import json
import secrets

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WebSettings:
    port: int = 7860
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    def authenticate(self, username: str, password: str) -> bool:
        if self.username is None or self.password is None:
            return False
        if not isinstance(username, str) or not isinstance(password, str):
            return False
        # compare_digest для строк поддерживает только ASCII. Сравниваем UTF-8.
        valid_name = secrets.compare_digest(username.encode(), self.username.encode())
        valid_password = secrets.compare_digest(password.encode(), self.password.encode())
        return valid_name and valid_password


def load_web_settings() -> WebSettings:
    load_dotenv(ROOT / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    username = os.getenv("WEB_AUTH_USERNAME", "").strip()
    password = os.getenv("WEB_AUTH_PASSWORD", "")
    required = os.getenv("RENDER", "").lower() == "true"
    if required or username or password:
        if not username or len(username) > 100 or not 16 <= len(password) <= 200:
            raise ConfigError("Задайте WEB_AUTH_USERNAME и WEB_AUTH_PASSWORD (от 16 до 200 символов) для тестового доступа.")
        return WebSettings(positive_int("PORT", 7860, 65535), username, password)
    return WebSettings(positive_int("PORT", 7860, 65535))


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


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    key: str = field(repr=False)
    timeout: int = 15

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.url)
            valid_url = (url.scheme == "https" and bool(url.hostname)
                         and not url.username and not url.password and not url.query
                         and not url.fragment and url.path in {"", "/"})
            _ = url.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigError("SUPABASE_URL: требуется HTTPS-адрес проекта без пути и параметров.")
        if not 20 <= len(self.key) <= 4096 or any(c.isspace() for c in self.key):
            raise ConfigError("SUPABASE_KEY: задайте серверный secret key.")
        if not self.key.startswith("sb_secret_"):
            # Локальная проверка типа legacy-ключа, не проверка его подписи.
            # Подлинность и права проверяет Supabase при запросе.
            try:
                parts = self.key.split(".")
                if len(parts) != 3:
                    raise ValueError
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                if payload.get("role") != "service_role":
                    raise ValueError
            except (ValueError, TypeError, AttributeError, UnicodeError):
                raise ConfigError("SUPABASE_KEY: нужен secret key или legacy service_role, не anon/publishable.") from None
        if type(self.timeout) is not int or not 1 <= self.timeout <= 120:
            raise ConfigError("SUPABASE_TIMEOUT_SECONDS: допустимо от 1 до 120.")


def load_supabase_settings() -> SupabaseSettings:
    """Загрузка БД не требует ключа LLM и не меняет его конфигурацию."""
    load_dotenv(ROOT / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    return SupabaseSettings(
        url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        key=os.getenv("SUPABASE_KEY", "").strip(),
        timeout=positive_int("SUPABASE_TIMEOUT_SECONDS", 15, 120),
    )


@dataclass(frozen=True)
class TelegramSettings:
    token: str = field(repr=False)
    chat_id: str = field(repr=False)
    timeout: int = 10
    include_contact: bool = False
    max_attempts: int = 5
    lease_seconds: int = 120

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", self.token):
            raise ConfigError("TELEGRAM_BOT_TOKEN: задайте токен из BotFather.")
        if not re.fullmatch(r"-?[1-9][0-9]{0,18}", self.chat_id):
            raise ConfigError("TELEGRAM_CHAT_ID: нужен числовой ID личного чата или группы.")
        if type(self.include_contact) is not bool:
            raise ConfigError("TELEGRAM_INCLUDE_CONTACT: true или false.")
        if type(self.timeout) is not int or not 1 <= self.timeout <= 30:
            raise ConfigError("TELEGRAM_TIMEOUT_SECONDS: от 1 до 30.")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ConfigError("NOTIFICATION_MAX_ATTEMPTS: от 1 до 10.")
        if type(self.lease_seconds) is not int or not 120 <= self.lease_seconds <= 600:
            raise ConfigError("NOTIFICATION_LEASE_SECONDS: от 120 до 600.")


def load_telegram_settings() -> TelegramSettings:
    load_dotenv(ROOT / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    include = os.getenv("TELEGRAM_INCLUDE_CONTACT", "false").strip().lower()
    if include not in {"true", "false"}:
        raise ConfigError("TELEGRAM_INCLUDE_CONTACT: true или false.")
    return TelegramSettings(
        token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        timeout=positive_int("TELEGRAM_TIMEOUT_SECONDS", 10, 30),
        include_contact=include == "true",
        max_attempts=positive_int("NOTIFICATION_MAX_ATTEMPTS", 5, 10),
        lease_seconds=positive_int("NOTIFICATION_LEASE_SECONDS", 120, 600),
    )


@dataclass(frozen=True)
class JudgeSettings:
    mode: str = "local"
    max_answer_chars: int = 4000

    def __post_init__(self) -> None:
        if self.mode not in {"local","llm"}:
            raise ConfigError("JUDGE_MODE: local или llm.")
        if type(self.max_answer_chars) is not int or not 512 <= self.max_answer_chars <= 8000:
            raise ConfigError("JUDGE_MAX_ANSWER_CHARS: от 512 до 8000.")


def load_judge_settings() -> JudgeSettings:
    load_dotenv(ROOT/".env",override=False,interpolate=False,encoding="utf-8-sig")
    return JudgeSettings(os.getenv("JUDGE_MODE","local").strip().lower(),
                         positive_int("JUDGE_MAX_ANSWER_CHARS",4000,8000))
