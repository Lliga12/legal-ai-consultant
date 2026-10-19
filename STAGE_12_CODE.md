# Полный код подготовки этапа 12

Снимок изменённых исходников для тестового размещения. Пароли и действующие ключи не включены.

## render.yaml

```yaml
# Первый запуск — только тестовый веб-сервис, без создания платного worker.
services:
  - type: web
    name: legal-ai-consultant
    runtime: python
    plan: free
    numInstances: 1
    buildCommand: python -m pip install -r requirements.txt
    startCommand: python app.py
    healthCheckPath: /
    autoDeployTrigger: "off"
    envVars:
      - key: WEB_CONCURRENCY
        value: "1"
      - key: GRADIO_ANALYTICS_ENABLED
        value: "False"
      - key: PYTHONUNBUFFERED
        value: "1"
      - key: PYTHONIOENCODING
        value: utf-8
      - key: WEB_AUTH_USERNAME
        sync: false
      - key: WEB_AUTH_PASSWORD
        sync: false
      - key: LLM_PROVIDER
        value: gemini
      - key: JUDGE_MODE
        value: local
      - key: LLM_REQUESTS_PER_MINUTE
        value: "5"
      - key: LLM_MAX_REQUESTS_PER_PROCESS
        value: "20"
      - key: TELEGRAM_INCLUDE_CONTACT
        value: "false"

```

## .python-version

```text
3.11

```

## .gitignore

```text
.env
.env.*
!.env.example
.venv/
venv/
__pycache__/
*.py[cod]
.pytest_cache/
.coverage
htmlcov/
*.log
.idea/
.vscode/
.gradio/
*.pem
*.key
*.p12
*.pfx
credentials*.json
service-account*.json
stage12-smoke-*.log

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
# Judge: local без API; llm требует серверного разрешения передачи обезличенного контекста.
JUDGE_MODE=local
JUDGE_MAX_ANSWER_CHARS=4000
# БД: серверный Secret key (sb_secret_...), не publishable/anon.
SUPABASE_URL=
SUPABASE_KEY=
SUPABASE_TIMEOUT_SECONDS=15
# Telegram: бот и числовой ID адресата (только сервер).
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_TIMEOUT_SECONDS=10
# По умолчанию телефон маскируется; true включает полный контакт в сообщении.
TELEGRAM_INCLUDE_CONTACT=false
NOTIFICATION_MAX_ATTEMPTS=5
NOTIFICATION_LEASE_SECONDS=120
# Gradio: один процесс, без публичного туннеля; пока только синтетические данные.
PORT=7860
# На Render обязательны: тестовый логин и пароль от 16 символов.
# Локально можно оставить пустыми. Пароль не связан с GitHub/Render.
WEB_AUTH_USERNAME=
WEB_AUTH_PASSWORD=
# Утверждённая политика оператора (HTTPS). Пока не предоставлена.
PRIVACY_POLICY_URL=

```

## config.py

```python
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

```

## ui/gradio_app.py

```python
"""Русскоязычный Gradio. На этапе 10 разрешено только тестирование."""

import atexit
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
from dotenv import load_dotenv

from config import (ROOT, ConfigError, load_judge_settings, load_settings,
                    load_supabase_settings, load_web_settings)
from core.assistant import Assistant
from core.judge import Judge
from core.memory import HistoryConsentRequired, Memory
from core.prompts import DISCLAIMER
from database.repositories import Repositories, SessionCredentials
from database.supabase_client import ConflictError, DatabaseError, SupabaseClient
from llm.factory import create_llm


WELCOME = "Здравствуйте! Я помогу уточнить юридический вопрос, стоимость и оформить заявку на консультацию."
# Встроенный clear Gradio меняет только виджет, не БД. Оставляем явную кнопку
# удаления истории с подтверждением; прочие встроенные toolbar-кнопки выключены.
UI_CSS = "#legal-chat button.icon-button { display: none !important; }"
TEST_NOTICE = (
    "**Тестовая версия. Используйте только вымышленные данные.** Например, имя Тест и телефон +70000000000. "
    "Реальные персональные данные пока не принимаются. После согласия переписка и этап записи сохраняются "
    "в настроенной Supabase; обезличенный текст общего вопроса передаётся выбранному AI-провайдеру. "
    "Автоматическое удаление всех косвенных идентификаторов не гарантируется. "
    "Доступ к диалогу сохраняется в этом браузере: не используйте общее устройство. "
    "Очистка истории сбрасывает согласие и черновик; сохранённые заявки удаляются отдельно."
)
LEAD_NOTICE = (
    "Согласие на тестовую заявку: имя, телефон, тема и описание будут сохранены в Supabase "
    "для обработки обращения. В Telegram юристу через отдельный обработчик передаются ID, дата, "
    "первое слово имени, общая тема и маскированный контакт (либо полный при серверной настройке). "
    "Это отдельное действие от согласия на переписку. Уже доставленное сообщение Telegram "
    "кнопка удаления заявки не удаляет. Полноценные тексты политики и согласий с реквизитами "
    "оператора должны быть подготовлены до публичного запуска."
)


@dataclass
class Runtime:
    assistant: Assistant | None = None
    issue: str = ""
    # BrowserState передаёт ключ шифрования фронтенду: это не серверный секрет
    # и не авторизация. Право доступа даёт отдельный 256-битный token каждой сессии.
    browser_secret: str = field(default="legal-ai-test-browser-storage-v1", repr=False)
    policy_url: str | None = None


def load_runtime() -> Runtime:
    load_dotenv(ROOT / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    runtime = Runtime()
    policy = os.getenv("PRIVACY_POLICY_URL", "").strip()
    if policy:
        url = urlsplit(policy)
        if url.scheme != "https" or not url.netloc or url.username or url.password or any(c in policy for c in '\"<> []'):
            runtime.issue = "PRIVACY_POLICY_URL: нужна корректная ссылка HTTPS."
            return runtime
        runtime.policy_url = policy
    client = llm = None
    try:
        settings, db_settings, judge_settings = load_settings(), load_supabase_settings(), load_judge_settings()
        client = SupabaseClient(db_settings)
        repo = Repositories(client)
        repo.health()
        repo.memory_health()
        repo.tools_health()
        repo.notifications_health()
        llm = create_llm(settings)
        runtime.assistant = Assistant(Memory(repo), llm, Judge(judge_settings, llm))
        atexit.register(client.close)
        atexit.register(llm.close)
    except ConfigError:
        runtime.issue = "Проверьте .env: настройки модели, Supabase и Judge не заполнены или некорректны."
    except DatabaseError:
        runtime.issue = "Supabase недоступен или не применены schema.sql и миграции 002–004. Диалог не включён."
    finally:
        if runtime.assistant is None:
            if client:
                client.close()
            if llm:
                llm.close()
    return runtime


def credentials_from_browser(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"session_id", "token"}:
        raise ValueError("Доступ к сохранённой сессии некорректен.")
    credentials = SessionCredentials(value["session_id"], value["token"])
    credentials.parameters()
    return credentials


def browser_value(credentials):
    return {"session_id": credentials.session_id, "token": credentials.token} if credentials else None


def chat_value(snapshot):
    return [{"role": "assistant", "content": WELCOME}] + [
        {"role": message.role, "content": message.content} for message in snapshot.messages]


def booking_value(snapshot):
    state = snapshot.state
    stages = {"idle": "запись не начата", "topic": "тема и описание", "name": "имя",
              "phone": "телефон", "consent": "проверка данных и согласие"}
    return "\n".join(("Этап: " + stages[state.booking_stage.value],
        "Тема: " + (state.legal_topic or "—"), "Описание: " + (state.booking_description or "—"),
        "Имя: " + (state.client_name or "—"), "Телефон: " + (state.booking_phone or "—")))


def create_app(runtime: Runtime | None = None):
    runtime = runtime or load_runtime()
    assistant = runtime.assistant

    def ready():
        if assistant is None:
            raise gr.Error(runtime.issue or "Сервер ещё не настроен.")

    def safe(action):
        try:
            ready()
            return action()
        except (HistoryConsentRequired, ConflictError) as exc:
            raise gr.Error(str(exc)) from None
        except DatabaseError:
            raise gr.Error("Не удалось подтвердить операцию в БД. Восстановите диалог; повтор заявки использует тот же черновик.") from None
        except (ValueError, TypeError, AttributeError):
            raise gr.Error("Проверьте ввод и доступ к сессии. Если диалог устарел, восстановите его.") from None

    def start(consent, saved):
        def operation():
            if consent is not True:
                raise HistoryConsentRequired("Перед началом подтвердите условия тестирования и хранения истории.")
            credentials = credentials_from_browser(saved) or assistant.memory.repositories.new_credentials()
            snapshot = assistant.start(credentials, consent=True)
            return credentials, snapshot.version, browser_value(credentials), chat_value(snapshot), "Диалог готов.", booking_value(snapshot)
        return safe(operation)

    def restore(saved):
        if saved is None or assistant is None:
            return None, None, [{"role": "assistant", "content": WELCOME}], runtime.issue or "Подтвердите условия и начните диалог.", "", False
        def operation():
            credentials = credentials_from_browser(saved)
            snapshot = assistant.memory.load(credentials)
            return credentials, snapshot.version, chat_value(snapshot), "Диалог восстановлен из БД.", booking_value(snapshot), snapshot.state.history_allowed
        return safe(operation)

    def send(text, credentials, version):
        def operation():
            if credentials is None:
                raise HistoryConsentRequired("Сначала подтвердите условия и начните диалог.")
            turn = assistant.send(credentials, text, expected_version=version)
            return chat_value(turn.snapshot), turn.notice or "Ответ подготовлен. Информация предварительная.", booking_value(turn.snapshot), turn.snapshot.version, "", False
        return safe(operation)

    def confirm(consent, credentials, version):
        def operation():
            if credentials is None:
                raise HistoryConsentRequired("Сначала начните диалог.")
            turn = assistant.confirm(credentials, expected_version=version, consent=consent)
            return chat_value(turn.snapshot), turn.notice or "Операция обработана.", booking_value(turn.snapshot), turn.snapshot.version, False
        return safe(operation)

    def clear(confirmed, credentials, version, leads=False):
        def operation():
            if confirmed is not True:
                raise HistoryConsentRequired("Подтвердите удаление отдельным флажком.")
            if credentials is None:
                raise HistoryConsentRequired("Сначала начните или восстановите диалог.")
            snapshot = assistant.clear(credentials, expected_version=version, leads=leads)
            return chat_value(snapshot), ("Заявки и задачи уведомлений удалены. Переписка сохранена." if leads else
                "История и черновик очищены. Перед новым сообщением снова подтвердите условия."), booking_value(snapshot), snapshot.version, snapshot.state.history_allowed, False, False
        return safe(operation)

    with gr.Blocks(title="AI-консультант юриста", analytics_enabled=False) as demo:
        gr.Markdown("# AI-консультант юриста\nСмоленск · консультации для физических лиц по всей России")
        gr.Markdown(DISCLAIMER)
        if assistant is not None and runtime.issue:
            gr.Markdown(runtime.issue)
        gr.Markdown(TEST_NOTICE, elem_id="test-data")
        if runtime.policy_url:
            gr.Markdown(f"[Политика обработки персональных данных]({runtime.policy_url})")
        else:
            gr.Markdown("Политика обработки персональных данных: ссылка оператора пока не задана. Реальные данные вводить нельзя.")
        gr.Markdown("[Условия обработки тестовых данных](#test-data)")
        credentials = gr.State(None)
        version = gr.State(None)
        saved = gr.BrowserState(None, storage_key="legal_ai_test_session_v1", secret=runtime.browser_secret)
        status = gr.Textbox(value=runtime.issue or "Подтвердите условия и начните диалог.", label="Состояние", interactive=False)
        history_consent = gr.Checkbox(False, label="Использую вымышленные данные и согласен на сохранение переписки и передачу общего вопроса AI-провайдеру")
        with gr.Row():
            begin = gr.Button("Начать диалог", variant="primary", interactive=assistant is not None)
            reload_button = gr.Button("Восстановить диалог", interactive=assistant is not None)
        new_button = gr.Button("Новый диалог — забыть доступ к прежнему")
        gr.Markdown("Новый диалог убирает доступ из браузера, но не удаляет данные в БД. Сначала удалите ненужные заявки и историю кнопками ниже.")
        chat = gr.Chatbot(value=[{"role": "assistant", "content": WELCOME}], label="Переписка",
                          height=400, sanitize_html=True, render_markdown=False, buttons=[],
                          feedback_options=(), allow_file_downloads=False, elem_id="legal-chat")
        text = gr.Textbox(label="Ваш вопрос", placeholder="Например: как взыскать алименты?", max_length=1500, lines=2, interactive=assistant is not None)
        send_button = gr.Button("Отправить", variant="primary", interactive=assistant is not None)
        with gr.Accordion("Заявка", open=True):
            preview = gr.Textbox(label="Данные заявки — проверьте перед подтверждением", interactive=False, lines=6)
            gr.Markdown(LEAD_NOTICE)
            lead_consent = gr.Checkbox(False, label="Согласен на обработку указанных тестовых данных для заявки")
            confirm_button = gr.Button("Подтвердить заявку", interactive=assistant is not None)
        with gr.Accordion("Управление данными", open=False):
            gr.Markdown("Очистка истории сохраняет зарегистрированные заявки. Удаление заявок сохраняет переписку. Доставленные уведомления Telegram остаются у получателя.")
            delete_confirmation = gr.Checkbox(False, label="Подтверждаю выбранное удаление")
            with gr.Row():
                clear_button = gr.Button("Очистить историю", interactive=assistant is not None)
                delete_button = gr.Button("Удалить мои заявки", interactive=assistant is not None)
        # Все операции одной группы сериализованы; API скрыт, но каждый вызов
        # всё равно проверяет владение сессией и версию в БД.
        event = dict(api_visibility="private", concurrency_id="dialog", concurrency_limit=1, show_progress="full")
        begin.click(start, [history_consent, saved], [credentials, version, saved, chat, status, preview], **event)
        restore_outputs = [credentials, version, chat, status, preview, history_consent]
        reload_button.click(restore, [saved], restore_outputs, **event)
        new_button.click(lambda: (None, None, None, [{"role": "assistant", "content": WELCOME}],
            "Доступ к прежнему диалогу убран из этого браузера. Подтвердите условия для нового.", "", False, False),
            [], [credentials, version, saved, chat, status, preview, history_consent, lead_consent], **event)
        demo.load(restore, [saved], restore_outputs, **event)
        outputs = [chat, status, preview, version, text, lead_consent]
        send_button.click(send, [text, credentials, version], outputs, **event)
        text.submit(send, [text, credentials, version], outputs, **event)
        confirm_button.click(confirm, [lead_consent, credentials, version], [chat, status, preview, version, lead_consent], **event)
        deletion_outputs = [chat, status, preview, version, history_consent, lead_consent, delete_confirmation]
        clear_button.click(clear, [delete_confirmation, credentials, version], deletion_outputs, **event)
        delete_button.click(lambda ok, c, v: clear(ok, c, v, leads=True), [delete_confirmation, credentials, version], deletion_outputs, **event)
    demo.queue(max_size=32, default_concurrency_limit=1)
    return demo


def launch():
    settings = load_web_settings()
    create_app().launch(server_name="0.0.0.0", server_port=settings.port, share=False,
        auth=settings.authenticate if settings.username is not None else None,
        auth_message="Тестовый доступ к AI-консультанту. Используйте только вымышленные данные.",
        show_error=False, inbrowser=False, strict_cors=True, footer_links=[],
        state_session_capacity=1000,
        css=UI_CSS,
        blocked_paths=[str(ROOT / ".env"), str(ROOT / ".git"), str(ROOT / "database")])

```

## app.py

```python
"""Gradio, проверки и обработчик уведомлений (этапы 1–10)."""

import argparse
from pathlib import Path
import sys


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Требуется Python 3.10 или новее.")
    parser = argparse.ArgumentParser(description="AI-консультант юриста: этап 10")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-llm", action="store_true", help="Один реальный тестовый запрос к LLM API")
    mode.add_argument("--check-router", action="store_true", help="Примеры маршрутизации без сети")
    mode.add_argument("--route", metavar="TEXT", help="Определить намерение без сети")
    mode.add_argument("--check-db", action="store_true", help="Проверить Supabase и ограничения схемы без записи данных")
    mode.add_argument("--check-memory", action="store_true", help="Проверить Supabase и миграцию Memory без записи данных")
    mode.add_argument("--check-tools", action="store_true", help="Проверить миграции инструментов без создания заявки")
    mode.add_argument("--check-notifications", action="store_true", help="Проверить схему очереди без отправки")
    mode.add_argument("--check-telegram", action="store_true", help="Проверить бота и чат без сообщений")
    mode.add_argument("--test-telegram", action="store_true", help="Отправить одно техническое сообщение в настроенный чат")
    mode.add_argument("--send-notifications", action="store_true", help="Обработать до 10 задач уведомления")
    mode.add_argument("--notification-worker", action="store_true", help="Обрабатывать очередь до Ctrl+C")
    mode.add_argument("--check-judge", action="store_true", help="Проверить Judge на локальных примерах без API")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    required = ("knowledge", "core", "llm", "tools", "database", "ui", "tests")
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise SystemExit("Отсутствуют каталоги: " + ", ".join(missing))
    if args.check_judge:
        try:
            from config import JudgeSettings
            from core.judge import Judge, JudgeContext
            from core.router import Intent
        except ImportError:
            raise SystemExit("Установите зависимости: python -m pip install -r requirements.txt") from None
        try:
            judge = Judge(JudgeSettings(mode="local"))
            cases = (
                ("Стоимость", "Консультация стоит 5000 рублей.", JudgeContext("Сколько стоит консультация?",intent=Intent.PRICE)),
                ("Гарантия", "Гарантируем взыскание алиментов.", JudgeContext("Как взыскать алименты?")),
                ("Действие", "Заявка зарегистрирована.", JudgeContext("Хочу записаться",intent=Intent.BOOKING)),
                ("Обычный вопрос", "Для взыскания алиментов уточните, есть ли уже решение суда.", JudgeContext("Как взыскать алименты?")),
            )
            for label,draft,context in cases:
                result = judge.review(draft,context)
                print(f"{label}: " + ("заменён" if result.replaced else "проверки пройдены"))
                if result.issues:
                    print("Причины: " + ", ".join(issue.value for issue in result.issues))
                print(result.answer)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        return
    if args.check_telegram or args.test_telegram or args.send_notifications or args.notification_worker:
        try:
            from config import ConfigError, load_telegram_settings, load_supabase_settings
            from database.repositories import Repositories
            from database.supabase_client import DatabaseError, SupabaseClient
            from tools.telegram_tools import TelegramClient, TelegramError
            from tools.notification_worker import NotificationWorker
        except ImportError:
            raise SystemExit("Установите зависимости: python -m pip install -r requirements.txt") from None
        try:
            with TelegramClient(load_telegram_settings()) as telegram:
                if args.check_telegram:
                    telegram.check()
                    print("Telegram: бот и чат доступны. Отправка сообщения не проверялась.")
                elif args.test_telegram:
                    message_id = telegram.send_text("Техническая проверка AI-консультанта юриста. Данные клиентов не передавались.")
                    print(f"Telegram подтвердил техническое сообщение, message_id={message_id}.")
                else:
                    import time
                    with SupabaseClient(load_supabase_settings()) as client:
                        repo = Repositories(client)
                        repo.health()
                        repo.memory_health()
                        repo.tools_health()
                        repo.notifications_health()
                        worker = NotificationWorker(repo,telegram)
                        while True:
                            results = worker.run_batch()
                            for result in results:
                                print("Уведомление: " + result.status,flush=True)
                            if not args.notification_worker:
                                print(f"Обработано задач: {len(results)}.")
                                break
                            time.sleep(2)
        except KeyboardInterrupt:
            print("Обработчик остановлен. Незавершённые задачи сохраняются в БД.")
        except (ConfigError,DatabaseError,TelegramError) as exc:
            raise SystemExit(str(exc)) from None
        return
    if args.check_db or args.check_memory or args.check_tools or args.check_notifications:
        try:
            from config import ConfigError, load_supabase_settings
            from database.repositories import Repositories
            from database.supabase_client import DatabaseError, SupabaseClient
        except ImportError:
            raise SystemExit("Установите зависимости: python -m pip install -r requirements.txt") from None
        try:
            with SupabaseClient(load_supabase_settings()) as client:
                repositories = Repositories(client)
                repositories.health()
                if args.check_memory or args.check_tools or args.check_notifications:
                    repositories.memory_health()
                if args.check_tools or args.check_notifications:
                    repositories.tools_health()
                if args.check_notifications:
                    repositories.notifications_health()
            print("Supabase ответил. Схема v1: RLS включён, доступ anon/authenticated закрыт.")
            if args.check_memory or args.check_tools or args.check_notifications:
                print("RPC Memory v1 доступны только серверной роли.")
            if args.check_tools or args.check_notifications:
                print("RPC Tool Calling v1 и черновики заявок доступны только серверной роли.")
            if args.check_notifications:
                print("Очередь Telegram и RPC доставки прошли проверку прав.")
            print("Проверка не создавала сессий, сообщений и заявок.")
        except (ConfigError, DatabaseError) as exc:
            raise SystemExit(str(exc)) from None
        return
    if args.check_router or args.route is not None:
        try:
            from core.router import Router
            from llm.base import Message
        except ImportError:
            raise SystemExit("Установите зависимости: python -m pip install -r requirements.txt") from None
        router = Router()
        examples = [(args.route, ())] if args.route is not None else [
            ("Сколько стоит развод?", ()),
            ("Хочу записаться на консультацию", ()),
            ("Как взыскать алименты?", ()),
            ("Привет", ()),
            ("А сколько?", (Message("user", "Как взыскать алименты?"),)),
            ("Анна", (Message("user", "Хочу записаться"),
                      Message("assistant", "Как вас зовут?"))),
        ]
        for text, history in examples:
            try:
                result = router.route(text, history)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            print(f"Намерение: {result.intent.value}; причина: {result.reason}")
            if result.secondary_intents:
                print("Дополнительно: " + ", ".join(i.value for i in result.secondary_intents))
            if result.legal_topic:
                print("Тема: " + result.legal_topic)
            if result.clarification:
                print(result.clarification)
            if result.booking_cancelled or result.consent_refused:
                print("Признак остановки оформления: да. Действия с заявкой не выполнялись.")
        return
    if not args.check_llm:
        from config import ConfigError
        from ui.gradio_app import launch
        try:
            launch()
        except ConfigError as exc:
            raise SystemExit(str(exc)) from None
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

## tests/test_deployment.py

```python
"""Защита тестового размещения: без сети и действующих ключей."""

from unittest.mock import patch

import pytest

from config import ConfigError, WebSettings, load_web_settings


def settings(env):
    with patch.dict("os.environ", env, clear=True), patch("config.load_dotenv"):
        return load_web_settings()


def test_local_start_without_auth_is_preserved():
    assert settings({}) == WebSettings()


@pytest.mark.parametrize("env", [
    {"RENDER": "true"},
    {"RENDER": "true", "WEB_AUTH_USERNAME": "tester"},
    {"RENDER": "true", "WEB_AUTH_PASSWORD": "a" * 20},
    {"RENDER": "true", "WEB_AUTH_USERNAME": "tester", "WEB_AUTH_PASSWORD": "short"},
    {"WEB_AUTH_USERNAME": "tester"},
    {"WEB_AUTH_PASSWORD": "a" * 20},
])
def test_incomplete_credentials_fail_closed_without_printing_password(env):
    with pytest.raises(ConfigError) as error:
        settings(env)
    assert env.get("WEB_AUTH_PASSWORD", "not-present") not in str(error.value)


def test_render_uses_port_and_exact_password_without_exposing_secrets():
    configured = settings({"RENDER": "true", "PORT": "10000",
        "WEB_AUTH_USERNAME": "Тест", "WEB_AUTH_PASSWORD": "Проверка-пароля-123456"})
    assert configured.port == 10000
    assert configured.authenticate("Тест", "Проверка-пароля-123456")
    assert not configured.authenticate("Другой", "Проверка-пароля-123456")
    assert not configured.authenticate("Тест", "wrong")
    assert not configured.authenticate(None, None)
    assert "Проверка" not in repr(configured)


@pytest.mark.parametrize("port", ["0", "65536", "text"])
def test_bad_render_port_is_rejected(port):
    with pytest.raises(ConfigError):
        settings({"PORT": port})

```

## tests/check_web_local.py

```python
"""HTTP-проверка входа Gradio без LLM, Supabase и Telegram."""

from pathlib import Path
import secrets
import socket
import sys
from unittest.mock import patch
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from config import load_web_settings
    from ui.gradio_app import Runtime, UI_CSS, create_app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = secrets.token_urlsafe(32)
    with patch.dict("os.environ", {"RENDER": "true", "PORT": str(port),
        "WEB_AUTH_USERNAME": "test-user", "WEB_AUTH_PASSWORD": password}), patch("config.load_dotenv"):
        settings = load_web_settings()
    demo = create_app(Runtime(issue="Local deployment check: integrations disabled."))
    try:
        demo.launch(server_name="127.0.0.1", server_port=settings.port,
            auth=settings.authenticate, share=False, inbrowser=False,
            prevent_thread_lock=True, quiet=True, footer_links=[], css=UI_CSS,
            blocked_paths=[str(ROOT / ".env"), str(ROOT / ".git"), str(ROOT / "database")])
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15) as client:
            assert client.get("/").status_code == 200
            assert client.get("/config").status_code == 401
            assert client.post("/login", data={"username": "test-user", "password": "wrong"}).status_code == 400
            assert client.post("/login", data={"username": "test-user", "password": password}).status_code == 200
            config = client.get("/config")
            assert config.status_code == 200
            assert password not in config.text
            path = quote(str(ROOT / "database/schema.sql"), safe="")
            assert client.get("/gradio_api/file=" + path).status_code in {403, 404}
        print("PASS: Gradio HTTP login, anonymous API rejection, password not exposed, SQL files blocked")
    finally:
        demo.close()


if __name__ == "__main__":
    main()

```

## tools/check_release.py

```python
"""Проверяет индекс Git перед отправкой. Значения возможных секретов не печатает."""

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    r"\bAIza[A-Za-z0-9_-]{30,}",
    r"\bsk-[A-Za-z0-9_-]{32,}",
    r"\bsb_secret_[A-Za-z0-9_-]{20,}",
    r"(?<![0-9])[0-9]{6,}:[A-Za-z0-9_-]{30,}",
)


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    failures = []
    count = 0
    for name in filter(None, paths):
        path = Path(name)
        if (any(part in {".venv", "venv", "__pycache__", ".pytest_cache", ".git"} for part in path.parts)
                or (path.name.startswith(".env") and path.name != ".env.example")
                or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".log"}):
            failures.append(name + ": forbidden release path")
            continue
        # Читаем именно staged-содержимое, а не иную рабочую версию.
        data = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
        count += 1
        if len(data) > 5_000_000:
            failures.append(name + ": unexpectedly large file")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if any(re.search(pattern, line) for pattern in PATTERNS):
                failures.append(f"{name}:{number}: possible credential; inspect locally")
    if not count:
        raise SystemExit("No staged files to check")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"PASS: {count} staged files checked; no forbidden paths or obvious credentials")


if __name__ == "__main__":
    main()

```
