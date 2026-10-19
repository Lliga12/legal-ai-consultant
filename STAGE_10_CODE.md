# Полный код этапа 10

Снимок исходных файлов этапа. Действующих ключей и данных клиентов нет; тестовые примеры синтетические.

## core/assistant.py

```python
"""Сборка диалога. Решения о согласии и действиях принимает только сервер."""

from dataclasses import dataclass, field, replace
from threading import RLock
from uuid import uuid4

from config import JudgeSettings

from core.judge import ActionEvidence, Judge, JudgeContext
from core.memory import HistoryConsentRequired, Memory, MemorySnapshot
from core.prompts import PRICE_STATEMENT, build_assistant_prompt, load_knowledge
from core.router import BookingStage, Intent, Router
from database.repositories import SessionCredentials, text_value
from database.supabase_client import ConflictError
from llm.base import BaseLLM, LLMError, Message
from tools.lead_tools import LeadTools, normalize_phone, validated_data


@dataclass(frozen=True)
class Turn:
    snapshot: MemorySnapshot = field(repr=False)
    answer: str = field(repr=False)
    notice: str = ""


class Assistant:
    """Один общий экземпляр на процесс: общий API-бюджет и полосатые блокировки.

    Версия БД дополнительно отвергает события из устаревших вкладок. Запускать
    одним процессом; межпроцессная аренда полного хода пока не реализована.
    """

    def __init__(self, memory: Memory, llm: BaseLLM, judge: Judge, *,
                 history_version="test-history-v1", consent_version="test-lead-v1"):
        self.memory, self.llm, self.judge = memory, llm, judge
        self.history_version, self.consent_version = history_version, consent_version
        self.router = Router()
        self.template_judge = Judge(JudgeSettings(mode="local"))
        self.system = build_assistant_prompt(load_knowledge())
        self._locks = tuple(RLock() for _ in range(64))

    def _lock(self, credentials):
        credentials.parameters()
        return self._locks[int(credentials.session_id.replace("-", ""), 16) % len(self._locks)]

    def start(self, credentials, *, consent: bool):
        with self._lock(credentials):
            return self.memory.initialize(credentials, history_consent=consent,
                                          notice_version=self.history_version)

    def _load(self, credentials, expected_version, *, need_consent=True):
        snapshot = self.memory.load(credentials)
        if need_consent and (not snapshot.state.history_allowed or snapshot.state.history_notice_version != self.history_version):
            raise HistoryConsentRequired("Сначала подтвердите условия тестирования и хранения истории.")
        if type(expected_version) is not int or snapshot.version != expected_version:
            raise ConflictError("Диалог изменился. Нажмите «Восстановить диалог» перед повтором.")
        return snapshot

    def _append(self, credentials, snapshot, role, content):
        return self.memory.append(credentials, message_id=str(uuid4()), role=role,
                                  content=content, expected_version=snapshot.version)

    @staticmethod
    def _private(state):
        return tuple(v for v in (state.client_name, state.booking_phone) if v)

    def _review(self, draft, question, intent, state, actions=None, refused=False, cloud=False):
        context = JudgeContext(question, intent=intent, facts=state.facts,
            private_values=self._private(state), actions=actions or ActionEvidence(),
            contact_collection_allowed=state.history_allowed and not refused,
            consent_refused=refused, cloud_review_allowed=cloud)
        return (self.judge if cloud else self.template_judge).review(draft, context).answer

    def send(self, credentials: SessionCredentials, text: str, *, expected_version: int) -> Turn:
        text = text_value(text, "Сообщение", 1500)
        with self._lock(credentials):
            snapshot = self._load(credentials, expected_version)
            state = snapshot.state
            route = self.router.route(text, snapshot.messages, booking_stage=state.booking_stage)
            snapshot = self._append(credentials, snapshot, "user", text)
            notice = ""
            intent = route.intent
            if route.booking_cancelled or route.consent_refused:
                # Отзываем согласие черновика; его данные остаются до очистки. Заявки
                # удаляются отдельной кнопкой с понятным подтверждением.
                if state.lead_request_id:
                    tool = LeadTools(self.memory.repositories, credentials, self.consent_version)
                    request = tool.get_request(state.lead_request_id)
                    tool.record_consent(request.request_id, consent=False,
                                        expected_version=snapshot.version)
                    snapshot = self.memory.load(credentials)
                state = replace(state, booking_stage=BookingStage.IDLE, client_name=None,
                    booking_description=None, booking_phone=None, lead_request_id=None)
                draft = "Оформление остановлено. Можно обсудить общий вопрос без контактов."
                intent = Intent.BOOKING
            elif intent == Intent.PRICE:
                state = replace(state, legal_topic=route.legal_topic or state.legal_topic)
                draft = PRICE_STATEMENT + " Для расчёта можно оформить заявку на консультацию."
                if Intent.BOOKING in route.secondary_intents and state.booking_stage == BookingStage.IDLE:
                    state = replace(state, booking_stage=BookingStage.TOPIC)
                    draft += " Кратко опишите тему и обстоятельства обращения."
            elif state.booking_stage != BookingStage.IDLE and intent == Intent.BOOKING:
                intent = Intent.BOOKING
                if state.booking_stage == BookingStage.TOPIC:
                    state = replace(state, legal_topic=route.legal_topic or state.legal_topic or "Юридическая консультация",
                        booking_description=text, booking_stage=BookingStage.NAME)
                    draft = "Укажите имя для заявки. Используйте только вымышленное имя в тестировании."
                elif state.booking_stage == BookingStage.NAME:
                    try:
                        validated_data(text, "+70000000000", state.legal_topic or "Консультация", "Тест", self.consent_version)
                        state = replace(state, client_name=text, booking_stage=BookingStage.PHONE)
                        draft = "Укажите телефон для заявки. Используйте только вымышленный номер."
                    except ValueError:
                        draft = "Укажите имя буквами, без цифр и других данных."
                elif state.booking_stage == BookingStage.PHONE:
                    try:
                        phone = normalize_phone(text)
                        tool = LeadTools(self.memory.repositories, credentials, self.consent_version)
                        request = tool.prepare(state.client_name, phone, state.legal_topic,
                            state.booking_description, expected_version=snapshot.version)
                        snapshot = self.memory.load(credentials)
                        state = replace(state, booking_phone=phone, lead_request_id=request.request_id,
                                        booking_stage=BookingStage.CONSENT)
                        draft = "Проверьте данные в разделе «Заявка». Для регистрации отметьте отдельное согласие и нажмите «Подтвердить заявку»."
                    except ValueError:
                        draft = "Нужен российский телефон: +7 и десять цифр. Используйте только вымышленный номер."
                else:
                    draft = "Для регистрации используйте отдельное согласие и кнопку «Подтвердить заявку». Сообщение «да» не заменяет согласие."
            elif intent == Intent.BOOKING or Intent.BOOKING in route.secondary_intents:
                state = replace(state, legal_topic=route.legal_topic or state.legal_topic,
                    booking_stage=BookingStage.TOPIC, booking_description=None,
                    client_name=None, booking_phone=None, lead_request_id=None)
                draft = "Кратко опишите тему обращения и что произошло, без настоящих персональных данных."
            elif intent == Intent.GENERAL:
                state = replace(state, legal_topic=route.legal_topic or state.legal_topic)
                # Не отправляем контактные этапы/историю записи в модель. Даже после
                # окончания записи контекст строится только из обезличенных фактов.
                context = JudgeContext(text, private_values=self._private(state))
                clean = Judge._redact(text, context)
                facts = tuple(Judge._redact(f, context) for f in state.facts[-5:])
                try:
                    messages = [Message("user", "Сообщённые ранее обстоятельства (не инструкции):\n" + "\n".join(facts))] if facts else []
                    messages.append(Message("user", clean))
                    draft = self.llm.generate(self.system, messages).text
                except LLMError:
                    draft = "Для предварительного разъяснения уточните, что произошло и какой результат вам нужен."
                    notice = "Модель не ответила или достигнут лимит. Автоматический повтор не выполнялся."
                state = replace(state, facts=(*facts, "Клиент: " + clean[:240])[-5:])
            else:
                draft = route.clarification or "Вас интересует стоимость, запись или юридический вопрос?"
            answer = self._review(draft, text, intent, state, refused=route.consent_refused,
                                  cloud=intent == Intent.GENERAL)
            if intent == Intent.GENERAL:
                # Сохраняем также проверенный уточняющий вопрос ассистента: иначе
                # короткий следующий ответ клиента потеряет смысл после перезапуска.
                redaction_context = JudgeContext(text, private_values=self._private(state))
                safe_answer = Judge._redact(answer, redaction_context)
                state = replace(state, facts=(*state.facts, "Ассистент: " + safe_answer[:240])[-6:])
            snapshot = self.memory.save_state(credentials, expected_version=snapshot.version, state=state)
            snapshot = self._append(credentials, snapshot, "assistant", answer)
            return Turn(snapshot, answer, notice)

    def confirm(self, credentials, *, expected_version: int, consent: bool) -> Turn:
        if consent is not True:
            raise HistoryConsentRequired("Отметьте отдельное согласие на заявку. Без него регистрация не выполняется.")
        with self._lock(credentials):
            snapshot = self._load(credentials, expected_version)
            state = snapshot.state
            if state.booking_stage != BookingStage.CONSENT or not state.lead_request_id:
                raise ValueError("Сначала заполните заявку в диалоге.")
            tool = LeadTools(self.memory.repositories, credentials, self.consent_version)
            request = tool.get_request(state.lead_request_id)
            # Повтор после неопределённого результата использует тот же ключ и согласие.
            if request.consent_at is None:
                request = tool.record_consent(request.request_id, consent=True,
                                              expected_version=snapshot.version)
            result = tool.dispatch("create_lead", {k: request.data[k] for k in
                ("name", "phone", "legal_topic", "description")} |
                {"session_id": credentials.session_id, "consent": True}, request_id=request.request_id)
            snapshot = self.memory.load(credentials)
            evidence = ActionEvidence(lead=result)
            draft = " ".join(evidence.sentences())
            if result.success:
                draft += " Время консультации нужно согласовать с юристом."
            else:
                draft += " Подтверждение регистрации отсутствует. Повторите эту же операцию позже."
            answer = self._review(draft, "Хочу записаться на консультацию", Intent.BOOKING,
                                  state, actions=evidence)
            snapshot = self._append(credentials, snapshot, "assistant", answer)
            return Turn(snapshot, answer,
                "Уведомление остаётся в очереди; доставка выполняется отдельным обработчиком."
                if result.success and result.notification_status != "sent" else "")

    def clear(self, credentials, *, expected_version, leads=False):
        with self._lock(credentials):
            self._load(credentials, expected_version, need_consent=False)
            if leads:
                self.memory.repositories.delete_leads(credentials)
                snapshot = self.memory.load(credentials)
                if not snapshot.state.history_allowed:
                    return snapshot
                state = replace(snapshot.state, booking_stage=BookingStage.IDLE, client_name=None,
                    booking_description=None, booking_phone=None, lead_request_id=None)
                return self.memory.save_state(credentials, expected_version=snapshot.version, state=state)
            self.memory.clear_history(credentials)
            return self.memory.load(credentials)

```

## core/memory.py

```python
"""Постоянная память диалога поверх Supabase. Локального хранилища сессий нет."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json

from core.router import BookingStage, RouteResult, Router
from database.repositories import Repositories, SessionCredentials, text_value, uuid_text
from database.supabase_client import DatabaseError
from llm.base import Message


class MemoryDataError(DatabaseError):
    pass


class HistoryConsentRequired(ValueError):
    pass


@dataclass(frozen=True)
class ConversationState:
    legal_topic: str | None = field(default=None, repr=False)
    facts: tuple[str, ...] = field(default=(), repr=False)
    client_name: str | None = field(default=None, repr=False)
    booking_stage: BookingStage = BookingStage.IDLE
    history_notice_version: str | None = None
    history_consent_at: str | None = None
    booking_description: str | None = field(default=None, repr=False)
    booking_phone: str | None = field(default=None, repr=False)
    lead_request_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value, label, maximum in ((self.legal_topic, "Тема", 200),
                                       (self.client_name, "Имя", 100),
                                       (self.history_notice_version, "Версия уведомления", 100)):
            if value is not None:
                text_value(value, label, maximum)
        if self.booking_description is not None:
            text_value(self.booking_description, "Описание", 1500)
        if self.booking_phone is not None:
            from tools.lead_tools import normalize_phone
            if normalize_phone(self.booking_phone) != self.booking_phone:
                raise ValueError("Телефон состояния должен быть нормализован.")
        if self.lead_request_id is not None:
            uuid_text(self.lead_request_id)
        if not isinstance(self.booking_stage, BookingStage):
            raise ValueError("Недопустимый этап записи.")
        if not isinstance(self.facts, tuple) or len(self.facts) > 20:
            raise ValueError("Обстоятельства: кортеж не более 20 строк.")
        for fact in self.facts:
            text_value(fact, "Обстоятельство", 500)
        if (self.history_notice_version is None) != (self.history_consent_at is None):
            raise ValueError("Для согласия на историю нужны версия уведомления и дата.")
        if self.history_consent_at is not None:
            try:
                stamp = datetime.fromisoformat(self.history_consent_at.replace("Z", "+00:00"))
                if stamp.utcoffset() is None:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Дата согласия должна содержать часовой пояс.") from None
        if len(json.dumps(self.to_dict(), ensure_ascii=False).encode("utf-8")) > 7000:
            raise ValueError("Состояние Memory превышает 7000 байт.")

    @property
    def history_allowed(self) -> bool:
        return self.history_notice_version is not None and self.history_consent_at is not None

    def to_dict(self) -> dict:
        return {"memory_schema": 2, "legal_topic": self.legal_topic, "facts": list(self.facts),
                "client_name": self.client_name, "booking_stage": self.booking_stage.value,
                "history_notice_version": self.history_notice_version,
                "history_consent_at": self.history_consent_at,
                "booking_description": self.booking_description, "booking_phone": self.booking_phone,
                "lead_request_id": self.lead_request_id}

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationState":
        if not isinstance(data, dict):
            raise ValueError("Состояние Memory должно быть объектом.")
        if not data:
            return cls()
        required = {"memory_schema", "legal_topic", "facts", "client_name", "booking_stage",
                    "history_notice_version", "history_consent_at"}
        schema = data.get("memory_schema")
        extra = {"booking_description", "booking_phone", "lead_request_id"}
        if (type(schema) is not int or schema not in (1, 2)
                or set(data) != (required if schema == 1 else required | extra)):
            raise ValueError("Неизвестный формат состояния Memory.")
        if not isinstance(data["facts"], list):
            raise ValueError("Некорректные обстоятельства Memory.")
        return cls(data["legal_topic"], tuple(data["facts"]), data["client_name"],
                   BookingStage(data["booking_stage"]), data["history_notice_version"],
                   data["history_consent_at"], data.get("booking_description"),
                   data.get("booking_phone"), data.get("lead_request_id"))


@dataclass(frozen=True)
class MemorySnapshot:
    session_id: str
    version: int
    state: ConversationState = field(repr=False)
    messages: tuple[Message, ...] = field(repr=False)


class Memory:
    def __init__(self, repositories: Repositories, history_limit: int = 40):
        if type(history_limit) is not int or not 1 <= history_limit <= 100:
            raise ValueError("Лимит Memory: от 1 до 100 сообщений.")
        self.repositories = repositories
        self.history_limit = history_limit

    def _decode(self, data: dict, credentials: SessionCredentials) -> MemorySnapshot:
        try:
            session, rows = data["session"], data["messages"]
            if (session["id"] != credentials.session_id or "token_hash" in session
                    or type(session["version"]) is not int or session["version"] < 0
                    or not isinstance(rows, list) or len(rows) > self.history_limit):
                raise ValueError
            state = ConversationState.from_dict(session["state"])
            messages = []
            ids = set()
            sequence = 0
            for row in rows:
                message_id = uuid_text(row["id"])
                if (row["session_id"] != credentials.session_id or message_id in ids
                        or row["role"] not in {"user", "assistant"}
                        or type(row["sequence"]) is not int or row["sequence"] <= sequence):
                    raise ValueError
                content = text_value(row["content"], "Сообщение", 16000)
                messages.append(Message(row["role"], content))
                ids.add(message_id)
                sequence = row["sequence"]
            return MemorySnapshot(session["id"], session["version"], state, tuple(messages))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MemoryDataError("БД вернула некорректный или чужой контекст Memory.") from None

    def load(self, credentials: SessionCredentials) -> MemorySnapshot:
        return self._decode(self.repositories.memory_snapshot(credentials, self.history_limit), credentials)

    def initialize(self, credentials: SessionCredentials, *, history_consent: bool,
                   notice_version: str) -> MemorySnapshot:
        """Сервер вызывает после явного согласия на хранение истории, до сообщений."""
        if history_consent is not True:
            raise HistoryConsentRequired("Для сохранения истории требуется явное согласие.")
        notice_version = text_value(notice_version, "Версия уведомления", 100)
        self.repositories.create_session(credentials)
        snapshot = self.load(credentials)
        if (snapshot.state.history_allowed
                and snapshot.state.history_notice_version == notice_version):
            return snapshot
        state = replace(snapshot.state, history_notice_version=notice_version,
                        history_consent_at=datetime.now(timezone.utc).isoformat())
        self.repositories.update_state(credentials, snapshot.version, state.to_dict())
        return self.load(credentials)

    def save_state(self, credentials: SessionCredentials, *, expected_version: int,
                   state: ConversationState) -> MemorySnapshot:
        """Только проверенные сервером поля. Произвольный JSON модели сюда не передаётся."""
        if not isinstance(state, ConversationState):
            raise ValueError("Требуется ConversationState.")
        current = self.load(credentials)
        if not current.state.history_allowed:
            raise HistoryConsentRequired("Сначала получите согласие на хранение истории.")
        if (state.history_notice_version != current.state.history_notice_version
                or state.history_consent_at != current.state.history_consent_at):
            raise ValueError("Изменение согласия допускается только через initialize/clear_history.")
        self.repositories.update_state(credentials, expected_version, state.to_dict())
        return self.load(credentials)

    def append(self, credentials: SessionCredentials, *, message_id: str,
               role: str, content: str, expected_version: int) -> MemorySnapshot:
        return self._decode(self.repositories.memory_append(
            credentials, message_id=message_id, role=role, content=content,
            expected_version=expected_version, limit=self.history_limit), credentials)

    def route(self, credentials: SessionCredentials, message: str,
              router: Router | None = None) -> tuple[RouteResult, MemorySnapshot]:
        snapshot = self.load(credentials)
        result = (router or Router()).route(message, snapshot.messages,
                                           booking_stage=snapshot.state.booking_stage)
        if result.legal_topic is None and snapshot.state.legal_topic is not None:
            result = replace(result, legal_topic=snapshot.state.legal_topic)
        return result, snapshot

    def clear_history(self, credentials: SessionCredentials) -> int:
        # Сбрасывает также структурированное состояние и согласие на историю.
        return self.repositories.clear_history(credentials)["deleted_messages"]

    def delete_session(self, credentials: SessionCredentials) -> None:
        self.repositories.delete_session(credentials)

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
                    load_supabase_settings, positive_int)
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
    load_dotenv(ROOT / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    port = positive_int("PORT", 7860, 65535)
    create_app().launch(server_name="0.0.0.0", server_port=port, share=False,
        show_error=False, inbrowser=False, strict_cors=True, footer_links=[],
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
        from ui.gradio_app import launch
        launch()
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

## requirements.txt

```text
httpx==0.28.1
python-dotenv==1.2.1
pytest==8.4.2
gradio==6.30.0

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
# Утверждённая политика оператора (HTTPS). Пока не предоставлена.
PRIVACY_POLICY_URL=

```

## tests/test_assistant.py

```python
"""Границы сборки; постоянное хранение проверяет PostgreSQL-runner."""

from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from config import JudgeSettings
from core.assistant import Assistant
from core.judge import Judge
from core.memory import ConversationState, HistoryConsentRequired, Memory
from database.repositories import SessionCredentials


def make_assistant():
    return Assistant(Mock(spec=Memory), Mock(), Judge(JudgeSettings(mode="local")))


@pytest.mark.parametrize("value", [None, "", " " * 3, "x" * 1501])
def test_invalid_input_before_database(value):
    assistant = make_assistant()
    with pytest.raises(ValueError):
        assistant.send(SessionCredentials(str(uuid4()), "a" * 43), value, expected_version=0)
    assistant.memory.load.assert_not_called()


@pytest.mark.parametrize("consent", [False, None, "да", 1])
def test_confirmation_requires_boolean_true(consent):
    assistant = make_assistant()
    with pytest.raises(HistoryConsentRequired):
        assistant.confirm(None, expected_version=0, consent=consent)
    assistant.memory.load.assert_not_called()


def test_old_memory_state_is_read_and_upgraded():
    old = ConversationState().to_dict()
    old["memory_schema"] = 1
    for key in ("booking_description", "booking_phone", "lead_request_id"):
        old.pop(key)
    loaded = ConversationState.from_dict(old)
    assert loaded == ConversationState()
    assert loaded.to_dict()["memory_schema"] == 2


def test_booking_state_private_fields_hidden_in_repr():
    state = replace(ConversationState(), client_name="СекретноеИмя", booking_phone="+70000000000",
                    booking_description="СекретноеОписание", lead_request_id=str(uuid4()))
    assert "Секретное" not in repr(state) and "+70000000000" not in repr(state)


def test_unknown_memory_v2_field_rejected():
    data = ConversationState().to_dict() | {"consent": True}
    with pytest.raises(ValueError):
        ConversationState.from_dict(data)

```

## tests/assistant_postgres_checks.py

```python
"""Полный диалог с настоящим временным PostgreSQL и тестовым ответом LLM."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from config import JudgeSettings, SupabaseSettings
from core.assistant import Assistant
from core.judge import Judge
from core.memory import HistoryConsentRequired, Memory
from core.router import BookingStage
from database.repositories import Repositories
from database.supabase_client import ConflictError, SupabaseClient
from llm.base import BaseLLM, LLMResponse
from tests.memory_postgres_checks import make_transport


def run_checks(execute_sql):
    with SupabaseClient(SupabaseSettings("https://local-test.invalid", "sb_secret_" + "x" * 32),
                        make_transport(execute_sql)) as client:
        repo = Repositories(client)
        model = Mock(spec=BaseLLM)
        model.generate.return_value = LLMResponse(
            "Для взыскания алиментов уточните, есть ли уже решение суда.", "test", "test")
        assistant = Assistant(Memory(repo), model, Judge(JudgeSettings(mode="local")))
        a, b = repo.new_credentials(), repo.new_credentials()
        try:
            assistant.start(a, consent=False)
            raise AssertionError("Missing history consent accepted")
        except HistoryConsentRequired:
            pass
        assert execute_sql(f"select count(*) from public.chat_sessions where id='{a.session_id}';") == "0"
        sa = assistant.start(a, consent=True)
        sb = assistant.start(b, consent=True)
        general = assistant.send(a, "Как взыскать алименты?", expected_version=sa.version)
        assert "решение суда" in general.answer, general.answer
        assert general.snapshot.state.legal_topic
        sa = general.snapshot
        price = assistant.send(a, "Сколько стоит консультация?", expected_version=sa.version)
        assert "индивидуально" in price.answer
        assert model.generate.call_count == 1
        sa = price.snapshot
        for text, stage in (("Хочу записаться на консультацию", BookingStage.TOPIC),
                            ("Нужно взыскать алименты, решения суда пока нет", BookingStage.NAME),
                            ("Тест", BookingStage.PHONE),
                            ("+70000000000", BookingStage.CONSENT)):
            turn = assistant.send(a, text, expected_version=sa.version)
            sa = turn.snapshot
            assert sa.state.booking_stage == stage, (stage, sa.state.booking_stage, turn.answer)
            if stage == BookingStage.PHONE:
                assert "телефон" in turn.answer, turn.answer
        request_id = sa.state.lead_request_id
        # Текстовое «да» не даёт согласие на регистрацию.
        sa = assistant.send(a, "да", expected_version=sa.version).snapshot
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "0"
        try:
            assistant.confirm(a, expected_version=sa.version, consent=False)
            raise AssertionError("Missing booking consent accepted")
        except HistoryConsentRequired:
            pass
        # Восстановление состояния новым объектом сервиса.
        restarted = Assistant(Memory(repo), model, Judge(JudgeSettings(mode="local")))
        restored = restarted.memory.load(a)
        assert restored.state.lead_request_id == request_id
        assert restored.state.client_name == "Тест"
        assert restored.state.booking_phone == "+70000000000"
        registered = restarted.confirm(a, expected_version=restored.version, consent=True)
        assert "Заявка зарегистрирована." in registered.answer, registered.answer
        repeat = restarted.confirm(a, expected_version=registered.snapshot.version, consent=True)
        assert "зарегистрирована ранее" in repeat.answer, repeat.answer
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "1"
        assert execute_sql(f"select count(*) from public.notification_logs n join public.leads l on l.id=n.lead_id where l.session_id='{a.session_id}';") == "1"
        assert "Уведомление отправлено" not in registered.answer
        # Контакты и переписка записи не передаются в LLM при новом общем вопросе.
        turn = restarted.send(a, "Как взыскать алименты?", expected_version=repeat.snapshot.version)
        payload = repr(model.generate.call_args)
        assert "+70000000000" not in payload and "Тест" not in payload, payload
        assert "есть ли уже решение суда" in payload, payload
        model.generate.return_value = LLMResponse("Консультация юриста стоит 5000 рублей.", "test", "test")
        turn = restarted.send(a, "Как взыскать алименты?", expected_version=turn.snapshot.version)
        assert "5000" not in turn.answer and "предварительный" in turn.answer
        assert restarted.memory.load(b).messages == ()
        # Два одинаковых устаревших события не запускают два хода.
        def stale(_):
            try:
                return restarted.send(b, "Сколько стоит развод?", expected_version=sb.version)
            except ConflictError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(stale, range(2)))
        assert sum(r is not None for r in results) == 1
        deleted = restarted.clear(a, expected_version=turn.snapshot.version, leads=True)
        assert deleted.state.lead_request_id is None
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "0"
        assert deleted.messages  # отдельное удаление заявок не стирает переписку
        cleared = restarted.clear(a, expected_version=deleted.version)
        assert cleared.messages == () and not cleared.state.history_allowed
        try:
            restarted.send(a, "Привет", expected_version=cleared.version)
            raise AssertionError("Sending after clear without renewed consent")
        except HistoryConsentRequired:
            pass
        restarted.clear(a, expected_version=cleared.version, leads=True)
        c = repo.new_credentials()
        sc = restarted.start(c, consent=True)
        mixed = restarted.send(c, "Сколько стоит консультация? Хочу записаться.", expected_version=sc.version)
        assert "индивидуально" in mixed.answer and mixed.snapshot.state.booking_stage == BookingStage.TOPIC
        print("PASS: assembled assistant: staged booking, explicit consent, one lead/outbox, restart, contact minimization, stale events, deletion")


def serve_ui(execute_sql):
    """Стенд интерфейса: временная SQL-БД, LLM — заглушка, Telegram не отправляется."""
    from ui.gradio_app import Runtime, UI_CSS, create_app
    with SupabaseClient(SupabaseSettings("https://local-test.invalid", "sb_secret_" + "x" * 32),
                        make_transport(execute_sql)) as client:
        model = Mock(spec=BaseLLM)
        model.generate.return_value = LLMResponse(
            "Для взыскания алиментов уточните, есть ли уже решение суда.", "test", "test")
        assistant = Assistant(Memory(Repositories(client)), model, Judge(JudgeSettings(mode="local")))
        runtime = Runtime(assistant=assistant, issue="ЛОКАЛЬНЫЙ ТЕСТ: временная PostgreSQL; LLM подменена; Telegram не отправляется.")
        create_app(runtime).launch(server_name="127.0.0.1", server_port=7861, share=False,
                                   show_error=False, footer_links=[], css=UI_CSS)

```

## tests/check_schema_local.py

```python
"""Проверка настоящим PostgreSQL во временной БД, без Supabase и реальных данных.

Запуск: python tests/check_schema_local.py. Требуются initdb/pg_ctl/psql в PATH.
"""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    binaries = {name: shutil.which(name) for name in ("initdb", "pg_ctl", "psql")}
    if not all(binaries.values()):
        raise SystemExit("Нужны initdb, pg_ctl и psql в PATH.")
    folder = Path(tempfile.mkdtemp(prefix="legal-ai-pg-" )).resolve()
    # Этот путь создаётся tempfile, не берётся из пользовательского ввода.
    data = folder / "data"
    password_file = folder / "password"
    password = secrets.token_urlsafe(32)
    password_file.write_text(password, encoding="ascii")
    env = {**os.environ, "PGPASSWORD": password, "PGCLIENTENCODING": "UTF8"}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run(args: list, *, sql: str | None = None) -> str:
        # pg_ctl порождает сервер: на Windows PIPE может остаться открытым
        # у дочернего postgres после завершения pg_ctl. Файлы исключают это зависание.
        with tempfile.TemporaryFile(mode="w+b") as output, tempfile.TemporaryFile(mode="w+b") as error:
            result = subprocess.run([str(a) for a in args], input=sql, stdout=output, stderr=error,
                                    encoding="utf-8", errors="replace", env=env,
                                    creationflags=flags,
                                    timeout=180 if Path(args[0]).stem.lower() == "initdb" else 60)
            output.seek(0)
            error.seek(0)
            stdout = output.read().decode("utf-8", errors="replace")
            stderr = error.read().decode("utf-8", errors="replace")
        if result.returncode:
            # Здесь только синтетические данные; пароль не входит в аргументы.
            raise RuntimeError(stderr or stdout)
        return stdout.strip()

    psql = [binaries["psql"], "-X", "-qAt", "-h", "127.0.0.1", "-p", str(port),
            "-U", "legal_ai_test", "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
    started = False
    try:
        run([binaries["initdb"], "-D", data, "-U", "legal_ai_test", "--encoding=UTF8",
             "--locale=C", "--auth=scram-sha-256", "--pwfile", password_file,
             "--no-sync"])  # Только создание одноразового тестового кластера, не production.
        started = True
        run([binaries["pg_ctl"], "-D", data, "-l", folder / "server.log", "-w", "-t", "20",
             "-o", f"-h 127.0.0.1 -p {port}", "start"])
        run(psql, sql="create role anon; create role authenticated; create role service_role bypassrls;")
        run(psql, sql=(ROOT / "database/schema.sql").read_text(encoding="utf-8"))
        run(psql, sql=(ROOT / "tests/schema_checks.sql").read_text(encoding="utf-8"))
        print("PASS: SQL schema, RLS/grants, session isolation, message order, state conflict, consent, outbox rollback")

        sql = """set role service_role;
            select public.legal_ai_create_lead('00000000-0000-0000-0000-000000000001',repeat('a',64),
            '00000000-0000-0000-0000-000000000040',
            'Test','+70000000000','Test topic','Synthetic description',true,'test-v1')->>'id';"""
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda _: run(psql, sql=sql), range(6)))
        assert len(set(ids)) == 1, "Concurrent duplicate lead IDs differ"
        counts = run(psql, sql="select (select count(*) from public.leads), (select count(*) from public.notification_logs);")
        assert counts == "1|1", counts
        run(psql, sql="""set role service_role;
            select public.legal_ai_delete_session('00000000-0000-0000-0000-000000000001',repeat('a',64));
            do $$ begin
                assert (select count(*) from public.leads) = 0;
                assert (select count(*) from public.notification_logs) = 0;
                assert (select count(*) from public.chat_sessions) = 1;
            end $$;""")
        print("PASS: six concurrent retries create one lead/outbox; session deletion cascades and preserves other session")
        migration = (ROOT / "database/migrations/002_memory.sql").read_text(encoding="utf-8")
        run(psql, sql=migration)
        run(psql, sql=migration)  # Повторное применение не должно открывать права.
        sys.path.insert(0, str(ROOT))
        from tests.memory_postgres_checks import run_checks
        run_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        migration = (ROOT / "database/migrations/003_lead_tools.sql").read_text(encoding="utf-8")
        run(psql, sql=migration)
        run(psql, sql=migration)
        from tests.lead_postgres_checks import run_checks as run_lead_checks
        run_lead_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        migration = (ROOT / "database/migrations/004_telegram.sql").read_text(encoding="utf-8")
        run(psql, sql=migration)
        run(psql, sql=migration)
        from tests.notification_postgres_checks import run_checks as run_notification_checks
        run_notification_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        from tests.assistant_postgres_checks import run_checks as run_assistant_checks
        run_assistant_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
        if "--ui" in sys.argv:
            # Только явно выбранный локальный тестовый стенд. Не режим приложения.
            from tests.assistant_postgres_checks import serve_ui
            serve_ui(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
    finally:
        if started and (data / "postmaster.pid").exists():
            run([binaries["pg_ctl"], "-D", data, "-w", "-t", "20", "stop", "-m", "fast"])
        # Проверяем расположение перед рекурсивным удалением только тестового каталога.
        temp_root = Path(tempfile.gettempdir()).resolve()
        if folder.parent != temp_root or not folder.name.startswith("legal-ai-pg-"):
            raise RuntimeError("Unexpected temporary directory; cleanup refused")
        shutil.rmtree(folder)


if __name__ == "__main__":
    main()

```

## README.md

```markdown
# AI-консультант юриста

## Текущее состояние

Реализованы этапы 1–10: архитектура, база знаний, адаптеры LLM, Router, БД, Memory, заявки, Telegram, Judge и Gradio.
Адаптеры проверяются локально с подменой HTTP. Реальное подключение требует ключа
и доступного аккаунту имени модели; успешный запрос к провайдеру пока не подтверждён.
`python app.py` запускает веб-чат на http://127.0.0.1:7860. Без настроек LLM и БД
интерфейс показывает причину и отключает диалог. Пока используются только вымышленные данные.
Для Supabase подготовлена SQL-схема и серверный RPC-клиент. Облачное подключение
не подтверждено: настройки Supabase отсутствуют; инструкция — в STAGE_5.md.
Пройдено 153 Python-теста, включая 33 проверки Judge. Схема, миграции 002–004, права, Memory, инструмент заявок
и очередь уведомлений проверены на временном PostgreSQL 17.6. Telegram проверен с
подменой HTTP; реальная отправка ожидает локальной настройки бота и чата.
Сборка Assistant проверена с временной SQL-БД и тестовым LLM. В браузере проверены
постепенная запись, подтверждение и восстановление после обновления страницы.
Каждый следующий этап выполняется после подтверждения пользователя.

Инструкции установки, настройки и проверки модели: [STAGE_3.md](STAGE_3.md).
Полный код файлов этапа: [STAGE_3_CODE.md](STAGE_3_CODE.md).
Описание и проверка Router: [STAGE_4.md](STAGE_4.md).
Полный код файлов этапа 4: [STAGE_4_CODE.md](STAGE_4_CODE.md).
Настройка Supabase и действия владельца: [STAGE_5.md](STAGE_5.md).
Полный код файлов этапа 5: [STAGE_5_CODE.md](STAGE_5_CODE.md).
Память диалога и дополнительная миграция: [STAGE_6.md](STAGE_6.md).
Полный код файлов этапа 6: [STAGE_6_CODE.md](STAGE_6_CODE.md).
Оформление заявки, согласие и новая миграция: [STAGE_7.md](STAGE_7.md).
Полный код файлов этапа 7: [STAGE_7_CODE.md](STAGE_7_CODE.md).
Настройка Telegram и обработчика очереди: [STAGE_8.md](STAGE_8.md).
Полный код файлов этапа 8: [STAGE_8_CODE.md](STAGE_8_CODE.md).
Проверка ответов и режимы Judge: [STAGE_9.md](STAGE_9.md).
Полный код файлов этапа 9: [STAGE_9_CODE.md](STAGE_9_CODE.md).
Запуск веб-чата и ограничения: [STAGE_10.md](STAGE_10.md).
Полный код файлов этапа 10: [STAGE_10_CODE.md](STAGE_10_CODE.md).

База знаний находится в `knowledge/knowledge.txt` (UTF-8). Она содержит сведения
из исходного задания и отдельно обозначенный сценарий будущей регистрации заявки.
Числовые цены, реквизиты, сроки ответа и другие неизвестные условия не добавлены.
Judge и Assistant читают эту базу; её контекст включается в системную инструкцию модели;
правила в текстовом файле сами по себе не обеспечивают проверки и защиту данных.

Для проверки содержимого в PowerShell из каталога проекта:

```powershell
Get-Content -LiteralPath '.\knowledge\knowledge.txt' -Encoding UTF8
```

## Проверка в Windows PowerShell

Из каталога проекта:

```powershell
Set-Location 'C:\Users\user\Desktop\дима разработка\03-AI Assistant urist\legal_ai_consultant'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Требуется Python 3.10+. Ожидаемый адрес:

```text
http://127.0.0.1:7860
```

## Архитектура и границы ответственности

Поток запроса:

```text
Gradio → Assistant → Memory → Router
                        ↓
        Knowledge Base + адаптер LLM
                        ↓
        проверенное состояние записи → create_lead
                        ↓
        Supabase: заявка + задача уведомления
                        ↓
        Judge → сохранение ответа → Gradio

Задача уведомления → Telegram → журнал доставки
```

`Assistant` управляет диалогом. Модель предлагает текст ответа,
но не получает ключи и не выполняет произвольные действия. Сервер валидирует
аргументы инструментов и решает, разрешено ли действие.

| Планируемый файл | Ответственность | Этап |
| --- | --- | --- |
| `knowledge/knowledge.txt` | Только известные сведения о практике и условиях обращения | 2 |
| `config.py`, `.env.example` | Проверяемая конфигурация и шаблон без секретов | 3 |
| `llm/base.py` | Общий интерфейс генерации с ограничением размера ответа | 3 |
| `llm/gemini.py`, `llm/openai_adapter.py`, `llm/factory.py` | Провайдеры, тайм-ауты, выбор через конфигурацию | 3 |
| `core/router.py` | PRICE, BOOKING, GENERAL, OTHER с учётом истории | 4 |
| `database/schema.sql`, `database/supabase_client.py`, `database/repositories.py` | Схема, серверный клиент, операции хранения | 5 |
| `core/memory.py` | История, состояние записи и изоляция сессий | 6 |
| `tools/lead_tools.py` | Валидация, согласие, идемпотентное создание заявки | 7 |
| `tools/telegram_tools.py` | Доставка уведомлений и повторные попытки | 8 |
| `core/judge.py`, `core/prompts.py` | Проверки текста и инструкции модели | 9 |
| `core/assistant.py`, `ui/gradio_app.py` | Полный цикл диалога и интерфейс | 10 |
| `tests/test_router.py`, `tests/test_memory.py`, `tests/test_tools.py`, `tests/test_judge.py` | Сценарии, ошибки и изоляция пользователей | 11 |
| `requirements.txt`, `render.yaml` | Проверенные зависимости и развёртывание | 3–12 |

Файлы из таблицы создаются на соответствующих этапах, чтобы не выдавать пустые
заготовки за реализованные интеграции. `__init__.py` пока содержат только описание
назначения пакетов.

## Хранение и надёжность

- `chat_sessions`: идентификатор, время создания и обновления; дополнительно
  планируются хеш секрета доступа и состояние оформления заявки.
- `chat_messages`: сообщения, связанные с сессией, с ограничением контекста для LLM.
- `leads`: поля из задания, время согласия, статус; дополнительно версия согласия
  и уникальный ключ идемпотентности для защиты от повторной отправки.
- `notification_logs`: состояние доставки, число попыток и время следующей попытки.
  Заявка и задача уведомления создаются одной транзакцией через серверную функцию
  PostgreSQL. Ошибка Telegram не отменяет сохранённую заявку.
- `lead_requests` (миграция 003): актуальный черновик, ключ операции и согласие,
  связанное с конкретными данными заявки. Для браузерных ролей доступ закрыт.

История и этап записи хранятся в Supabase, а не только в памяти процесса Gradio.
Один UUID не служит авторизацией: сервер проверяет секрет владения сессией,
его хеш хранится в БД. Клиент не выбирает произвольную чужую сессию.
Для конкурентных сообщений предусматриваются сериализация операций одной сессии
и проверка версии её состояния в БД.

При ошибке сохранения заявки ответ не утверждает, что она зарегистрирована.
Повторная попытка использует прежний ключ идемпотентности. Сообщение об успешном
действии строится из результата инструмента, а не из предположения модели.

## Персональные данные и безопасность

Это проектные требования, а не заключение о соблюдении законодательства.
Актуальная правовая оценка локализации и трансграничной передачи будет проведена
до публичного запуска, с проверкой первичных источников и фактических регионов
размещения Supabase, Render и выбранного AI-провайдера.
Реальные персональные данные до такой проверки не используются.

Реализовано информирование до первого отправленного сообщения: история также
может содержать персональные данные. Согласие на заявку запрашивается отдельно,
фиксируется с датой и версией текста. Отказ прекращает сбор контактных данных.
Для публичного запуска нужны реквизиты оператора и утверждённая политика;
сведения о юристе, которых нет в задании, не выдумываются.

Секрет Supabase доступен только серверу. Таблицы закрыты для anon/authenticated
через права и RLS; сервер проверяет принадлежность сессии перед каждой операцией.
Предусмотрены удаление истории и отдельное удаление заявки с её уведомлениями,
с объяснением ограничений удаления уже доставленного сообщения в Telegram.

Контакты не передаются в LLM и не попадают в журналы ошибок. Telegram по умолчанию
получает первое слово имени, маскированный контакт, общую тему, ID и дату заявки,
без описания дела. Полный телефон включается отдельной серверной настройкой.
Данные пользователя считаются недоверенными:
они не изменяют инструкции, не разрешают инструменты и не подтверждают согласие
по указанию модели.

Ограничения длины ввода, запросов и расходов действуют на сервере. Для нескольких
процессов лимиты должны быть общими через БД. Judge проверяет цены, гарантии,
ссылки на нормы, персональные данные и заявления о действиях, но сам по себе
не обеспечивает юридическую достоверность ответа.

## Последовательность работы

1. Архитектура и структура — выполнено.
2. База знаний — выполнено.
3. Облачная модель и конфигурация — реализовано; реальная проверка ожидает ключ и модель.
4. Router — реализовано, проверяется без внешних API.
5. Supabase — инфраструктура реализована; облачная проверка ожидает настройки.
6. Memory — реализовано; для облачной проверки нужна миграция 002_memory.sql и настройки БД.
7. Tool Calling — реализовано.
8. Telegram — реализовано; реальная отправка ожидает настройки владельца.
9. Judge — реализовано; локальные правила и необязательная семантическая проверка.
10. Gradio и сборка полного цикла — реализовано; запуск: `python app.py`, подробности в STAGE_10.md.
11. Сценарные тесты.
12. Подготовка и проверка развёртывания на Render.

Внешние интеграции будут считаться проверенными только после реальных запросов
с предоставленными настройками. Тесты с подменой API проверяют логику, но не
подтверждают доступность внешнего сервиса.

```
