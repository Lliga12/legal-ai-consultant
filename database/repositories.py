"""Типизированная граница БД. Это инфраструктура, а не Memory или tool агента."""

from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
from uuid import UUID, uuid4

from database.supabase_client import DatabaseError, SupabaseClient


def uuid_text(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Требуется UUID.") from None


def text_value(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label}: требуется непустая строка до {maximum} символов.")
    return value.strip()


@dataclass(frozen=True)
class SessionCredentials:
    session_id: str
    token: str = field(repr=False)

    def parameters(self) -> dict:
        session_id = uuid_text(self.session_id)
        if not isinstance(self.token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.token):
            raise ValueError("Некорректный секрет сессии.")
        return {"p_session_id": session_id,
                "p_token_hash": hashlib.sha256(self.token.encode("ascii")).hexdigest()}


class Repositories:
    def __init__(self, client: SupabaseClient):
        self.client = client

    def _object(self, operation: str, parameters: dict) -> dict:
        value = self.client.rpc(operation, parameters)
        if not isinstance(value, dict):
            raise DatabaseError("Неверная структура результата БД.")
        if operation in {"create_session", "get_session", "update_state"}:
            if (value.get("id") != parameters["p_session_id"] or "token_hash" in value
                    or not isinstance(value.get("state"), dict)
                    or type(value.get("version")) is not int or value["version"] < 0):
                raise DatabaseError("Неверная структура сессии БД.")
        if operation in {"append_message", "create_lead"}:
            try:
                uuid_text(value.get("id"))
            except ValueError:
                raise DatabaseError("БД не подтвердила идентификатор записи.") from None
            if not isinstance(value.get("created_at"), str):
                raise DatabaseError("БД не подтвердила дату записи.")
        if operation == "create_lead" and type(value.get("duplicate")) is not bool:
            raise DatabaseError("БД не подтвердила результат регистрации заявки.")
        for operation_name, count_name in (("clear_history", "deleted_messages"),
                                           ("delete_leads", "deleted_leads")):
            if operation == operation_name and (type(value.get(count_name)) is not int or value[count_name] < 0):
                raise DatabaseError("БД не подтвердила удаление данных.")
        if operation == "delete_session" and value.get("deleted") is not True:
            raise DatabaseError("БД не подтвердила удаление сессии.")
        return value

    def health(self) -> dict:
        result = self._object("health", {})
        if (result.get("schema_version") != 1 or result.get("rls_enabled") is not True
                or result.get("public_access") is not False):
            raise DatabaseError("Схема БД или ограничения доступа не прошли проверку.")
        return result

    def new_credentials(self) -> SessionCredentials:
        return SessionCredentials(str(uuid4()), secrets.token_urlsafe(32))

    def create_session(self, credentials: SessionCredentials) -> dict:
        # Секрет создаётся до вызова: после тайм-аута можно повторить с тем же UUID/секретом.
        return self._object("create_session", credentials.parameters())

    def get_session(self, credentials: SessionCredentials) -> dict:
        return self._object("get_session", credentials.parameters())

    def append_message(self, credentials: SessionCredentials, message_id: str,
                       role: str, content: str) -> dict:
        if role not in {"user", "assistant"}:
            raise ValueError("Допустимы роли user и assistant.")
        return self._object("append_message", {
            **credentials.parameters(), "p_message_id": uuid_text(message_id), "p_role": role,
            "p_content": text_value(content, "Сообщение", 16000)})

    def messages(self, credentials: SessionCredentials, limit: int = 40) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Лимит истории: от 1 до 100.")
        result = self.client.rpc("messages", {**credentials.parameters(), "p_limit": limit})
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise DatabaseError("Неверная структура истории БД.")
        return result

    def update_state(self, credentials: SessionCredentials, expected_version: int, state: dict) -> dict:
        if type(expected_version) is not int or expected_version < 0 or not isinstance(state, dict):
            raise ValueError("Требуются версия состояния и JSON-объект.")
        try:
            size = len(json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, TypeError):
            raise ValueError("Состояние должно быть корректным JSON.") from None
        if size > 8000:
            raise ValueError("Состояние превышает 8000 байт.")
        return self._object("update_state", {**credentials.parameters(),
                            "p_expected_version": expected_version, "p_state": state})

    def create_lead(self, credentials: SessionCredentials, *, idempotency_key: str,
                    name: str, phone: str, legal_topic: str, description: str,
                    consent: bool, consent_version: str) -> dict:
        if consent is not True:
            raise ValueError("Для заявки требуется явное согласие.")
        if not isinstance(phone, str) or not re.fullmatch(r"\+7\d{10}", phone, re.ASCII):
            raise ValueError("Телефон должен быть нормализован: +7 и 10 цифр.")
        return self._object("create_lead", {
            **credentials.parameters(), "p_idempotency_key": uuid_text(idempotency_key),
            "p_name": text_value(name, "Имя", 100), "p_phone": phone,
            "p_legal_topic": text_value(legal_topic, "Тема", 200),
            "p_description": text_value(description, "Описание", 2000),
            "p_consent": True, "p_consent_version": text_value(consent_version, "Версия согласия", 100)})

    def clear_history(self, credentials: SessionCredentials) -> dict:
        return self._object("clear_history", credentials.parameters())

    def delete_leads(self, credentials: SessionCredentials) -> dict:
        return self._object("delete_leads", credentials.parameters())

    def delete_session(self, credentials: SessionCredentials) -> dict:
        return self._object("delete_session", credentials.parameters())

    def memory_health(self) -> dict:
        result = self._object("memory_health", {})
        if result.get("memory_version") != 1 or result.get("rpc_ready") is not True:
            raise DatabaseError("Миграция Memory или права её RPC не прошли проверку.")
        return result

    def memory_snapshot(self, credentials: SessionCredentials, limit: int = 40) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Лимит истории: от 1 до 100.")
        return self._object("memory_snapshot", {**credentials.parameters(), "p_limit": limit})

    def memory_append(self, credentials: SessionCredentials, *, message_id: str,
                      role: str, content: str, expected_version: int, limit: int = 40) -> dict:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Требуется неотрицательная версия Memory.")
        if role not in {"user", "assistant"}:
            raise ValueError("Допустимы роли user и assistant.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Лимит истории: от 1 до 100.")
        return self._object("memory_append", {
            **credentials.parameters(), "p_message_id": uuid_text(message_id), "p_role": role,
            "p_content": text_value(content, "Сообщение", 16000),
            "p_expected_version": expected_version, "p_limit": limit})

    def tools_health(self) -> dict:
        result = self._object("tools_health", {})
        if result.get("tools_version") != 1 or result.get("rpc_ready") is not True:
            raise DatabaseError("Миграция Tool Calling или права её RPC не прошли проверку.")
        return result

    def prepare_lead(self, credentials: SessionCredentials, expected_version: int, data: dict) -> dict:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("Требуется версия сессии.")
        data = self._lead_fields(data)
        return self._object("prepare_lead", {**credentials.parameters(),
            "p_expected_version": expected_version, **{"p_" + k: v for k, v in data.items()}})

    def lead_request(self, credentials: SessionCredentials, request_id: str) -> dict:
        return self._object("lead_request", {**credentials.parameters(),
                                             "p_request_id": uuid_text(request_id)})

    def lead_consent(self, credentials: SessionCredentials, request_id: str,
                     expected_version: int, consent: bool) -> dict:
        if type(consent) is not bool or type(expected_version) is not int or expected_version < 0:
            raise ValueError("Требуются явный выбор согласия и версия сессии.")
        return self._object("lead_consent", {**credentials.parameters(), "p_request_id": uuid_text(request_id),
                            "p_expected_version": expected_version, "p_consent": consent})

    def submit_lead(self, credentials: SessionCredentials, request_id: str, data: dict) -> dict:
        data = self._lead_fields(data)
        return self._object("submit_lead", {**credentials.parameters(), "p_request_id": uuid_text(request_id),
                                           "p_consent": True, **{"p_" + k: v for k, v in data.items()}})

    @staticmethod
    def _lead_fields(data: dict) -> dict:
        limits = {"name":100,"phone":12,"legal_topic":200,"description":2000,"consent_version":100}
        if not isinstance(data, dict) or set(data) != set(limits):
            raise ValueError("Недопустимые поля черновика заявки.")
        result = {key:text_value(data[key], key, maximum) for key, maximum in limits.items()}
        if not re.fullmatch(r"\+7[0-9]{10}", result["phone"], re.ASCII):
            raise ValueError("Требуется нормализованный телефон.")
        return result

    def notifications_health(self) -> dict:
        result = self._object("notifications_health", {})
        if result.get("notifications_version") != 1 or result.get("rpc_ready") is not True:
            raise DatabaseError("Миграция уведомлений или права её RPC не прошли проверку.")
        return result

    def claim_notification(self, lease_seconds: int = 120, max_attempts: int = 5) -> dict | None:
        if (type(lease_seconds) is not int or not 120 <= lease_seconds <= 600
                or type(max_attempts) is not int or not 1 <= max_attempts <= 10):
            raise ValueError("Недопустимые ограничения доставки.")
        result = self.client.rpc("claim_notification", {"p_lease_seconds":lease_seconds,
                                                        "p_max_attempts":max_attempts})
        if result is None:
            return None
        if not isinstance(result,dict):
            raise DatabaseError("БД вернула некорректную задачу уведомления.")
        try:
            uuid_text(result["id"])
            uuid_text(result["claim_token"])
            if type(result["attempts"]) is not int or not 1 <= result["attempts"] <= max_attempts:
                raise ValueError
        except (KeyError,ValueError):
            raise DatabaseError("БД вернула некорректную аренду задачи.") from None
        return result

    def notification_current(self, notification_id: str, claim_token: str) -> dict | None:
        result = self.client.rpc("notification_current", {"p_notification_id":uuid_text(notification_id),
                                                           "p_claim_token":uuid_text(claim_token)})
        if result is not None and not isinstance(result,dict):
            raise DatabaseError("БД вернула некорректную заявку для уведомления.")
        return result

    def finish_notification(self, notification_id: str, claim_token: str, *, sent: bool,
                            message_id: int | None = None, error_code: str | None = None,
                            retry_delay: int | None = None, cooldown_delay: int | None = None) -> bool:
        codes = {"connection_failed","delivery_unknown","response_unknown","api_400","api_401", "api_403",
                 "api_404","api_429","api_500","api_502","api_503","api_504","api_rejected",
                 "invalid_retry_after","unexpected_recipient","invalid_payload"}
        if (type(sent) is not bool or (sent and (type(message_id) is not int or message_id<=0
                or error_code is not None or retry_delay is not None or cooldown_delay is not None))
                or (not sent and (message_id is not None or error_code not in codes))
                or (retry_delay is not None and (type(retry_delay) is not int or not 1<=retry_delay<=604800))
                or (cooldown_delay is not None and (type(cooldown_delay) is not int
                    or not 1<=cooldown_delay<=604800 or error_code!="api_429"))):
            raise ValueError("Некорректный результат доставки.")
        result = self._object("finish_notification", {"p_notification_id":uuid_text(notification_id),
            "p_claim_token":uuid_text(claim_token),"p_sent":sent,"p_message_id":message_id,
            "p_error_code":error_code,"p_retry_delay":retry_delay,"p_cooldown_delay":cooldown_delay})
        if type(result.get("recorded")) is not bool:
            raise DatabaseError("БД не подтвердила запись результата доставки.")
        return result["recorded"]
