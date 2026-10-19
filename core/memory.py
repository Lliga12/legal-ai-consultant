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
