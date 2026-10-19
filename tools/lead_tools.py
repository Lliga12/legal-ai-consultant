"""Реальное серверное действие регистрации. Модель не может сама дать согласие."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re

from database.repositories import Repositories, SessionCredentials, text_value, uuid_text
from database.supabase_client import (ConflictError, DatabaseError, DataValidationError,
                                       SessionAccessError)


def normalize_phone(phone: str) -> str:
    if not isinstance(phone, str) or len(phone) > 40:
        raise ValueError("Укажите российский номер телефона.")
    phone = phone.strip().replace("\u00a0", " ")
    if not re.fullmatch(r"\+?[0-9 ()\-.]+", phone, re.ASCII):
        raise ValueError("Телефон может содержать только цифры и стандартные разделители.")
    digits = re.sub(r"[ ()\-.]", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "+7" + digits[1:]
    elif len(digits) == 11 and digits.startswith("7"):
        digits = "+" + digits
    elif len(digits) == 10 and not digits.startswith("+"):
        digits = "+7" + digits
    if not re.fullmatch(r"\+7[0-9]{10}", digits, re.ASCII):
        raise ValueError("Нужен российский номер: +7 и 10 цифр.")
    return digits


def validated_data(name: str, phone: str, legal_topic: str,
                   description: str, consent_version: str) -> dict:
    name = text_value(name, "Имя", 100)
    if not any(c.isalpha() for c in name) or any(not (c.isalpha() or c in " -'.") for c in name):
        raise ValueError("Имя должно содержать буквы, пробелы, дефис, точку или апостроф.")
    return {"name": name, "phone": normalize_phone(phone),
            "legal_topic": text_value(legal_topic, "Тема", 200),
            "description": text_value(description, "Описание", 2000),
            "consent_version": text_value(consent_version, "Версия согласия", 100)}


@dataclass(frozen=True)
class LeadRequest:
    request_id: str
    session_version: int
    data: dict = field(repr=False)
    consent_at: str | None = None
    lead_id: str | None = None


class LeadStatus(str, Enum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    VALIDATION_ERROR = "validation_error"
    CONSENT_REQUIRED = "consent_required"
    CONFLICT = "conflict"
    SESSION_UNAVAILABLE = "session_unavailable"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class LeadResult:
    status: LeadStatus
    message: str
    lead_id: str | None = None
    created_at: str | None = None
    notification_status: str | None = None

    @property
    def success(self) -> bool:
        return self.status in {LeadStatus.CREATED, LeadStatus.ALREADY_EXISTS}


class LeadTools:
    """Экземпляр привязан сервером к владельцу сессии и версии согласия."""

    def __init__(self, repositories: Repositories, credentials: SessionCredentials, consent_version: str):
        credentials.parameters()
        self.repositories = repositories
        self.credentials = credentials
        self.consent_version = text_value(consent_version, "Версия согласия", 100)

    def _request(self, data: dict) -> LeadRequest:
        try:
            request_id = uuid_text(data["request_id"])
            if data["session_id"] != self.credentials.session_id:
                raise ValueError
            if type(data["session_version"]) is not int or data["session_version"] < 0:
                raise ValueError
            fields = validated_data(*(data[k] for k in
                ("name", "phone", "legal_topic", "description", "consent_version")))
            consent_at = data.get("consent_at")
            if consent_at is not None:
                self._date(consent_at)
            lead_id = uuid_text(data["lead_id"]) if data.get("lead_id") is not None else None
            return LeadRequest(request_id, data["session_version"], fields, consent_at, lead_id)
        except (KeyError, ValueError, TypeError):
            raise DatabaseError("БД вернула некорректный черновик заявки.") from None

    @staticmethod
    def _date(value: str) -> None:
        if not isinstance(value, str):
            raise ValueError("Требуется дата.")
        # PostgreSQL убирает завершающие нули дробной части. Python 3.10
        # fromisoformat не принимает некоторые длины этой части (например 5 цифр).
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                datetime.strptime(value, fmt)
                return
            except ValueError:
                continue
        raise ValueError("Некорректная дата или отсутствует часовой пояс.")

    def prepare(self, name: str, phone: str, legal_topic: str, description: str, *,
                expected_version: int) -> LeadRequest:
        """Сервер готовит проверенный черновик; изменения сбрасывают согласие."""
        data = validated_data(name, phone, legal_topic, description, self.consent_version)
        return self._request(self.repositories.prepare_lead(self.credentials, expected_version, data))

    def get_request(self, request_id: str) -> LeadRequest:
        return self._request(self.repositories.lead_request(self.credentials, request_id))

    def record_consent(self, request_id: str, *, consent: bool, expected_version: int) -> LeadRequest:
        """Только обработчик явного выбора клиента. Не входит в доступные модели tools."""
        if type(consent) is not bool:
            raise ValueError("Требуется явный выбор согласия.")
        return self._request(self.repositories.lead_consent(
            self.credentials, request_id, expected_version, consent))

    def create_lead(self, name: str, phone: str, legal_topic: str, description: str,
                    session_id: str, consent: bool, *, request_id: str) -> LeadResult:
        """Шесть аргументов из ТЗ + request_id, привязанный доверенным сервером."""
        if consent is not True:
            return LeadResult(LeadStatus.CONSENT_REQUIRED, "Без явного согласия заявка не создаётся.")
        try:
            if uuid_text(session_id) != self.credentials.session_id:
                return LeadResult(LeadStatus.SESSION_UNAVAILABLE, "Сессия недоступна.")
            request_id = uuid_text(request_id)
            data = validated_data(name, phone, legal_topic, description, self.consent_version)
        except ValueError as exc:
            return LeadResult(LeadStatus.VALIDATION_ERROR, str(exc))
        try:
            result = self.repositories.submit_lead(self.credentials, request_id, data)
            # Успех только после идентификатора, даты и статуса из реального ответа БД.
            lead_id = uuid_text(result["id"])
            self._date(result["created_at"])
            if (type(result["duplicate"]) is not bool or result["notification_status"]
                    not in {"pending", "sending", "sent", "failed"}):
                raise ValueError
        except SessionAccessError:
            return LeadResult(LeadStatus.SESSION_UNAVAILABLE, "Сессия или черновик заявки недоступны.")
        except DataValidationError:
            return LeadResult(LeadStatus.CONSENT_REQUIRED, "Согласие на эту заявку не подтверждено.")
        except ConflictError:
            return LeadResult(LeadStatus.CONFLICT, "Данные заявки изменились. Проверьте черновик и согласие.")
        except (DatabaseError, ValueError, KeyError, TypeError):
            return LeadResult(LeadStatus.UNCONFIRMED,
                "Регистрация заявки не подтверждена. Повторная проверка должна использовать тот же черновик.")
        return LeadResult(LeadStatus.ALREADY_EXISTS if result["duplicate"] else LeadStatus.CREATED,
            "Заявка зарегистрирована ранее." if result["duplicate"] else "Заявка зарегистрирована.",
            lead_id, result["created_at"], result["notification_status"])

    def dispatch(self, tool_name: str, arguments: dict, *, request_id: str) -> LeadResult:
        """Whitelist для будущего Assistant. request_id берётся из серверного состояния."""
        fields = {"name", "phone", "legal_topic", "description", "session_id", "consent"}
        if tool_name != "create_lead" or not isinstance(arguments, dict) or set(arguments) != fields:
            return LeadResult(LeadStatus.VALIDATION_ERROR, "Неизвестный инструмент или недопустимые аргументы.")
        return self.create_lead(**arguments, request_id=request_id)
