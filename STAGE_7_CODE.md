# Полный код этапа 7

Снимок исходных файлов для просмотра. Рабочие версии находятся по указанным путям. Действующих ключей и секретов сессий нет.

## tools/lead_tools.py

```python
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
```

## database/migrations/003_lead_tools.sql

```sql
-- Этап 7. После schema.sql и 002_memory.sql. Повторное применение допустимо.
begin;

create table if not exists public.lead_requests (
    session_id uuid primary key references public.chat_sessions(id) on delete cascade,
    request_id uuid not null unique default gen_random_uuid(),
    name text not null check (char_length(btrim(name)) between 1 and 100),
    phone text not null check (phone ~ '^\+7[0-9]{10}$'),
    legal_topic text not null check (char_length(btrim(legal_topic)) between 1 and 200),
    description text not null check (char_length(btrim(description)) between 1 and 2000),
    consent_version text not null check (char_length(btrim(consent_version)) between 1 and 100),
    consent_at timestamptz,
    lead_id uuid references public.leads(id) on delete set null,
    created_at timestamptz not null default now()
);
alter table public.lead_requests enable row level security;
revoke all on public.lead_requests from public, anon, authenticated;
grant select, insert, update, delete on public.lead_requests to service_role;

create or replace function public.legal_ai_lead_request(
    p_session_id uuid, p_token_hash text, p_request_id uuid)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare d public.lead_requests;
declare s public.chat_sessions;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    select * into d from public.lead_requests
        where session_id = p_session_id and request_id = p_request_id;
    if not found then raise exception 'request unavailable' using errcode = 'P0002'; end if;
    return to_jsonb(d) || jsonb_build_object('session_version', s.version);
end;
$$;

create or replace function public.legal_ai_prepare_lead(
    p_session_id uuid, p_token_hash text, p_expected_version bigint,
    p_name text, p_phone text, p_legal_topic text, p_description text, p_consent_version text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
declare d public.lead_requests;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    if nullif(btrim(s.state->>'history_notice_version'), '') is null
        or nullif(btrim(s.state->>'history_consent_at'), '') is null then
        raise exception 'history consent required' using errcode = '22023';
    end if;
    select * into d from public.lead_requests where session_id = p_session_id;
    if found and d.name = p_name and d.phone = p_phone and d.legal_topic = p_legal_topic
        and d.description = p_description and d.consent_version = p_consent_version then
        return public.legal_ai_lead_request(p_session_id, p_token_hash, d.request_id);
    end if;
    if s.version is distinct from p_expected_version then
        raise exception 'stale draft version' using errcode = '40001';
    end if;
    -- Новый/изменённый черновик имеет новый ключ и требует нового согласия.
    delete from public.lead_requests where session_id = p_session_id;
    insert into public.lead_requests(session_id,name,phone,legal_topic,description,consent_version)
        values (p_session_id,p_name,p_phone,p_legal_topic,p_description,p_consent_version)
        returning * into d;
    update public.chat_sessions set version = version + 1, updated_at = now() where id = p_session_id;
    return public.legal_ai_lead_request(p_session_id, p_token_hash, d.request_id);
end;
$$;

create or replace function public.legal_ai_lead_consent(
    p_session_id uuid, p_token_hash text, p_request_id uuid,
    p_expected_version bigint, p_consent boolean)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    if s.version is distinct from p_expected_version then
        raise exception 'stale consent event' using errcode = '40001';
    end if;
    if p_consent is null then raise exception 'explicit consent required' using errcode = '22023'; end if;
    update public.lead_requests set consent_at = case when p_consent then now() else null end
        where session_id = p_session_id and request_id = p_request_id;
    if not found then raise exception 'request unavailable' using errcode = 'P0002'; end if;
    update public.chat_sessions set version = version + 1, updated_at = now() where id = p_session_id;
    return public.legal_ai_lead_request(p_session_id, p_token_hash, p_request_id);
end;
$$;

create or replace function public.legal_ai_submit_lead(
    p_session_id uuid, p_token_hash text, p_request_id uuid, p_name text, p_phone text,
    p_legal_topic text, p_description text, p_consent boolean, p_consent_version text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare d public.lead_requests;
declare result jsonb;
declare notification_status text;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    select * into d from public.lead_requests
        where session_id = p_session_id and request_id = p_request_id;
    if not found then raise exception 'request unavailable' using errcode = 'P0002'; end if;
    if p_consent is distinct from true or d.consent_at is null then
        raise exception 'confirmed consent required' using errcode = '22023';
    end if;
    if d.name is distinct from p_name or d.phone is distinct from p_phone
        or d.legal_topic is distinct from p_legal_topic or d.description is distinct from p_description
        or d.consent_version is distinct from p_consent_version then
        raise exception 'draft payload conflict' using errcode = '23505';
    end if;
    result := public.legal_ai_create_lead(p_session_id,p_token_hash,p_request_id,
        p_name,p_phone,p_legal_topic,p_description,true,p_consent_version);
    update public.lead_requests set lead_id = (result->>'id')::uuid where session_id = p_session_id;
    if not (result->>'duplicate')::boolean then
        update public.leads set consent_at = d.consent_at where id = (result->>'id')::uuid;
        update public.chat_sessions set version = version + 1, updated_at = now() where id = p_session_id;
    end if;
    select status into notification_status from public.notification_logs where lead_id = (result->>'id')::uuid;
    if not found then raise exception 'missing outbox' using errcode = 'P0001'; end if;
    return result || jsonb_build_object('notification_status',notification_status);
end;
$$;

-- Очистка контекста инвалидирует черновик, его согласие и старые попытки отправки.
create or replace function public.legal_ai_clear_history(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.lead_requests where session_id = p_session_id;
    delete from public.chat_messages where session_id = p_session_id;
    get diagnostics n = row_count;
    update public.chat_sessions set state = '{}'::jsonb, version = version + 1, updated_at = now()
        where id = p_session_id;
    return jsonb_build_object('deleted_messages', n);
end;
$$;

create or replace function public.legal_ai_delete_leads(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.lead_requests where session_id = p_session_id;
    delete from public.leads where session_id = p_session_id;
    get diagnostics n = row_count;
    update public.chat_sessions set version = version + 1, updated_at = now() where id = p_session_id;
    return jsonb_build_object('deleted_leads', n);
end;
$$;

create or replace function public.legal_ai_tools_health()
returns jsonb language sql security invoker set search_path = '' as $$
    select jsonb_build_object('tools_version',1,'rpc_ready',
        (select count(*) = 5 and not bool_or(p.prosecdef)
            and not bool_or(pg_catalog.has_function_privilege('anon',p.oid,'EXECUTE'))
            and not bool_or(pg_catalog.has_function_privilege('authenticated',p.oid,'EXECUTE'))
         from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid = p.pronamespace
         where n.nspname='public' and p.proname in ('legal_ai_lead_request','legal_ai_prepare_lead',
            'legal_ai_lead_consent','legal_ai_submit_lead','legal_ai_tools_health'))
        and (select c.relrowsecurity from pg_catalog.pg_class c
            where c.oid='public.lead_requests'::regclass)
        and not pg_catalog.has_table_privilege('anon','public.lead_requests','SELECT,INSERT,UPDATE,DELETE')
        and not pg_catalog.has_table_privilege('authenticated','public.lead_requests','SELECT,INSERT,UPDATE,DELETE'));
$$;

revoke all on function public.legal_ai_lead_request(uuid,text,uuid),
    public.legal_ai_prepare_lead(uuid,text,bigint,text,text,text,text,text),
    public.legal_ai_lead_consent(uuid,text,uuid,bigint,boolean),
    public.legal_ai_submit_lead(uuid,text,uuid,text,text,text,text,boolean,text),
    public.legal_ai_tools_health() from public, anon, authenticated;
grant execute on function public.legal_ai_lead_request(uuid,text,uuid),
    public.legal_ai_prepare_lead(uuid,text,bigint,text,text,text,text,text),
    public.legal_ai_lead_consent(uuid,text,uuid,bigint,boolean),
    public.legal_ai_submit_lead(uuid,text,uuid,text,text,text,text,boolean,text),
    public.legal_ai_tools_health() to service_role;
notify pgrst,'reload schema';
commit;
```

## database/repositories.py

```python
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
```

## database/supabase_client.py

```python
"""Серверный PostgREST RPC-клиент; секреты и тела ошибок не журналируются."""

import httpx

from config import SupabaseSettings


class DatabaseError(RuntimeError):
    pass


class SessionAccessError(DatabaseError):
    pass


class ConflictError(DatabaseError):
    pass


class DataValidationError(DatabaseError):
    pass


class SupabaseClient:
    def __init__(self, settings: SupabaseSettings, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._http = httpx.Client(timeout=settings.timeout, transport=transport,
                                  follow_redirects=False)

    def rpc(self, name: str, parameters: dict) -> object:
        allowed = {"health", "create_session", "get_session", "append_message", "messages",
                   "update_state", "create_lead", "clear_history", "delete_leads", "delete_session",
                   "memory_snapshot", "memory_append", "memory_health", "prepare_lead",
                   "lead_request", "lead_consent", "submit_lead", "tools_health"}
        if name not in allowed:
            raise ValueError("Неизвестная операция БД.")
        headers = {"apikey": self._settings.key, "Content-Type": "application/json"}
        # Новые secret keys не являются JWT: не отправляем их как Bearer.
        if not self._settings.key.startswith("sb_secret_"):
            headers["Authorization"] = "Bearer " + self._settings.key
        failed = False
        try:
            response = self._http.post(
                self._settings.url.rstrip("/") + "/rest/v1/rpc/legal_ai_" + name,
                headers=headers, json=parameters)
        except httpx.TimeoutException:
            reason = "Supabase не ответил вовремя. Результат записи неизвестен; автоматического повтора не было."
            failed = True
        except httpx.RequestError:
            reason = "Ошибка связи с Supabase. Результат операции не подтверждён."
            failed = True
        if failed:
            raise DatabaseError(reason)
        if not 200 <= response.status_code < 300:
            try:
                data = response.json()
                code = data.get("code") if isinstance(data, dict) else None
            except ValueError:
                code = None
            if code == "P0002":
                raise SessionAccessError("Сессия недоступна или секрет сессии неверен.")
            if code in {"40001", "23505"}:
                raise ConflictError("Конфликт состояния или ключа повторной операции.")
            if code == "22023":
                raise DataValidationError("БД отклонила данные или неподтверждённое согласие.")
            raise DatabaseError(f"Supabase вернул HTTP {response.status_code}. Операция не подтверждена.")
        try:
            return response.json()
        except ValueError:
            raise DatabaseError("Некорректный JSON от Supabase. Результат операции не подтверждён.") from None

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
```

## app.py

```python
"""Проверки структуры, LLM, Router, Supabase, Memory и Tools (этапы 1–7)."""

import argparse
from pathlib import Path
import sys


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Требуется Python 3.10 или новее.")
    parser = argparse.ArgumentParser(description="AI-консультант юриста: этап 7")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-llm", action="store_true", help="Один реальный тестовый запрос к LLM API")
    mode.add_argument("--check-router", action="store_true", help="Примеры маршрутизации без сети")
    mode.add_argument("--route", metavar="TEXT", help="Определить намерение без сети")
    mode.add_argument("--check-db", action="store_true", help="Проверить Supabase и ограничения схемы без записи данных")
    mode.add_argument("--check-memory", action="store_true", help="Проверить Supabase и миграцию Memory без записи данных")
    mode.add_argument("--check-tools", action="store_true", help="Проверить миграции инструментов без создания заявки")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    required = ("knowledge", "core", "llm", "tools", "database", "ui", "tests")
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise SystemExit("Отсутствуют каталоги: " + ", ".join(missing))
    if args.check_db or args.check_memory or args.check_tools:
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
                if args.check_memory or args.check_tools:
                    repositories.memory_health()
                if args.check_tools:
                    repositories.tools_health()
            print("Supabase ответил. Схема v1: RLS включён, доступ anon/authenticated закрыт.")
            if args.check_memory or args.check_tools:
                print("RPC Memory v1 доступны только серверной роли.")
            if args.check_tools:
                print("RPC Tool Calling v1 и черновики заявок доступны только серверной роли.")
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
        print("AI-консультант юриста — структура проекта готова.")
        print("Этап 7. Router: --check-router; LLM: --check-llm; БД: --check-db; Memory: --check-memory; Tools: --check-tools.")
        print("Веб-чат ещё не реализован.")
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

## tests/test_tools.py

```python
"""Граница Tool Calling: валидация, доверенные параметры и честный результат."""

from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from database.repositories import Repositories, SessionCredentials
from database.supabase_client import (ConflictError, DatabaseError, DataValidationError,
                                       SessionAccessError)
from tools.lead_tools import LeadStatus, LeadTools, normalize_phone


class LeadToolTests(TestCase):
    def setUp(self):
        self.repo = Mock(spec=Repositories)
        self.credentials = SessionCredentials(str(uuid4()), "a" * 43)
        self.tools = LeadTools(self.repo, self.credentials, "synthetic-consent-v1")
        self.request_id = str(uuid4())
        self.arguments = {"name":"Тестовое имя", "phone":"8 (000) 000-00-00", "legal_topic":"Тест",
                          "description":"Синтетическое описание", "session_id":self.credentials.session_id,
                          "consent":True}
        self.lead_id = str(uuid4())
        self.repo.submit_lead.return_value = {"id":self.lead_id,"duplicate":False,
            "created_at":"2026-10-16T12:00:00Z","notification_status":"pending"}
        self.draft = {"request_id":self.request_id,"session_id":self.credentials.session_id,
            "session_version":2,"name":"Тестовое имя","phone":"+70000000000","legal_topic":"Тест",
            "description":"Синтетическое описание","consent_version":"synthetic-consent-v1",
            "consent_at":None,"lead_id":None}

    def call(self, **changes):
        return self.tools.create_lead(**{**self.arguments, **changes}, request_id=self.request_id)

    def test_phone_normalization(self):
        for value in ("8 (000) 000-00-00", "70000000000", "+7 000 000 00 00", "0000000000"):
            self.assertEqual(normalize_phone(value), "+70000000000")
        for value in ("+1 000 000 0000", "phone", "+7１２３４５６７８９０", "123", None, "7" * 41):
            with self.assertRaises(ValueError):
                normalize_phone(value)

    def test_postgres_timestamps_with_trimmed_fractional_seconds(self):
        for fraction in ("", ".1", ".12", ".123", ".1234", ".12345", ".123456"):
            for zone in ("Z", "+00:00", "+03:00"):
                stamp = "2026-10-16T12:00:00" + fraction + zone
                self.repo.submit_lead.return_value["created_at"] = stamp
                self.assertTrue(self.call().success, stamp)
                self.draft["consent_at"] = stamp
                self.repo.lead_request.return_value = self.draft
                self.assertEqual(self.tools.get_request(self.request_id).consent_at, stamp)

    def test_valid_create_returns_db_id_and_outbox_state(self):
        result = self.call()
        self.assertTrue(result.success)
        self.assertEqual(result.status, LeadStatus.CREATED)
        self.assertEqual(result.lead_id, self.lead_id)
        self.assertEqual(result.notification_status, "pending")
        data = self.repo.submit_lead.call_args.args[2]
        self.assertEqual(data["phone"], "+70000000000")
        self.assertEqual(data["consent_version"], "synthetic-consent-v1")
        self.assertNotIn("отправлено", result.message.casefold())

    def test_duplicate_returns_existing_id(self):
        self.repo.submit_lead.return_value["duplicate"] = True
        result = self.call()
        self.assertEqual(result.status, LeadStatus.ALREADY_EXISTS)
        self.assertEqual(result.lead_id, self.lead_id)

    def test_refusal_does_not_call_database(self):
        for consent in (False, None, "true", 1):
            result = self.call(consent=consent)
            self.assertEqual(result.status, LeadStatus.CONSENT_REQUIRED)
            self.assertFalse(result.success)
        self.repo.submit_lead.assert_not_called()

    def test_wrong_session_never_reaches_database(self):
        self.assertEqual(self.call(session_id=str(uuid4())).status, LeadStatus.SESSION_UNAVAILABLE)
        self.repo.submit_lead.assert_not_called()

    def test_invalid_fields_no_database_call(self):
        for changes in ({"name":"<script>"},{"name":""},{"phone":"bad"},
                        {"legal_topic":"x"*201},{"description":"x"*2001}, {"description":"bad\x00value"}):
            self.assertEqual(self.call(**changes).status, LeadStatus.VALIDATION_ERROR)
        self.repo.submit_lead.assert_not_called()

    def test_errors_are_safe_and_never_successful(self):
        cases = ((DataValidationError,LeadStatus.CONSENT_REQUIRED),(ConflictError,LeadStatus.CONFLICT),
                 (SessionAccessError,LeadStatus.SESSION_UNAVAILABLE),(DatabaseError,LeadStatus.UNCONFIRMED))
        for error, status in cases:
            self.repo.submit_lead.side_effect = error("SECRET +70000000000")
            result = self.call()
            self.assertEqual(result.status, status)
            self.assertFalse(result.success)
            self.assertIsNone(result.lead_id)
            self.assertNotIn("SECRET", repr(result))
            self.assertNotIn("+70000000000", repr(result))

    def test_malformed_success_response_is_unconfirmed(self):
        for data in ({}, {"id":"not-uuid"}, {"id":self.lead_id,"duplicate":False,"created_at":"bad"},
                     {"id":self.lead_id,"duplicate":False,"created_at":"2026-10-16T12:00:00Z",
                      "notification_status":"delivered_by_magic"}):
            self.repo.submit_lead.return_value = data
            self.assertEqual(self.call().status, LeadStatus.UNCONFIRMED)

    def test_failed_notification_does_not_erase_saved_lead(self):
        self.repo.submit_lead.return_value["notification_status"] = "failed"
        result = self.call()
        self.assertTrue(result.success)
        self.assertEqual(result.lead_id, self.lead_id)
        self.assertEqual(result.notification_status, "failed")

    def test_prepare_normalizes_before_persistence(self):
        self.repo.prepare_lead.return_value = self.draft
        draft = self.tools.prepare("Тестовое имя","8 (000) 000-00-00","Тест","Синтетическое описание",
                                   expected_version=1)
        self.assertEqual(draft.request_id,self.request_id)
        self.assertNotIn("Тестовое имя",repr(draft))
        self.assertNotIn("+70000000000",repr(draft))
        self.assertEqual(self.repo.prepare_lead.call_args.args[2]["phone"],"+70000000000")

    def test_record_consent_requires_actual_boolean_and_version(self):
        self.repo.lead_consent.return_value = self.draft
        self.tools.record_consent(self.request_id, consent=False, expected_version=2)
        self.repo.lead_consent.assert_called_once_with(self.credentials,self.request_id,2,False)
        with self.assertRaises(ValueError):
            self.tools.record_consent(self.request_id, consent="true", expected_version=2)

    def test_foreign_draft_rejected(self):
        self.draft["session_id"] = str(uuid4())
        self.repo.lead_request.return_value = self.draft
        with self.assertRaises(DatabaseError):
            self.tools.get_request(self.request_id)

    def test_tool_dispatch_whitelist(self):
        for tool_name, arguments in (("record_consent",self.arguments),("delete_session",{}),
                                      ("create_lead",{**self.arguments,"consent_version":"forged"}),
                                      ("create_lead",{**self.arguments,"request_id":str(uuid4())})):
            result = self.tools.dispatch(tool_name, arguments, request_id=self.request_id)
            self.assertEqual(result.status,LeadStatus.VALIDATION_ERROR)
        self.repo.submit_lead.assert_not_called()

    def test_dispatch_uses_server_bound_request_id(self):
        self.tools.dispatch("create_lead",self.arguments,request_id=self.request_id)
        self.assertEqual(self.repo.submit_lead.call_args.args[1],self.request_id)

    def test_repo_rejects_credential_overrides(self):
        repo = Repositories(Mock())
        data = {key:self.draft[key] for key in ("name","phone","legal_topic","description","consent_version")}
        for extra in ({"session_id":str(uuid4())},{"token_hash":"forged"},{"consent":True}):
            with self.assertRaises(ValueError):
                repo.submit_lead(self.credentials,self.request_id,{**data,**extra})

    def test_health_check_contract(self):
        client = Mock()
        client.rpc.return_value = {"tools_version":1,"rpc_ready":True}
        Repositories(client).tools_health()
        client.rpc.return_value = {"tools_version":1,"rpc_ready":False}
        with self.assertRaises(DatabaseError):
            Repositories(client).tools_health()
```

## tests/lead_postgres_checks.py

```python
"""Проверка инструмента через настоящие SQL-транзакции временного PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from config import SupabaseSettings
from core.memory import Memory
from database.repositories import Repositories, SessionCredentials
from database.supabase_client import ConflictError, SupabaseClient
from tests.memory_postgres_checks import make_transport
from tools.lead_tools import LeadStatus, LeadTools


def run_checks(execute_sql) -> None:
    settings = SupabaseSettings("https://local-test.invalid","sb_secret_"+"x"*32)
    with SupabaseClient(settings,make_transport(execute_sql)) as client:
        repo = Repositories(client)
        repo.health()
        repo.memory_health()
        repo.tools_health()
        memory = Memory(repo)
        a,b = repo.new_credentials(),repo.new_credentials()
        snapshot = memory.initialize(a,history_consent=True,notice_version="synthetic-history-notice")
        memory.initialize(b,history_consent=True,notice_version="synthetic-history-notice")
        tools = LeadTools(repo,a,"synthetic-lead-consent")
        values = dict(name="Тест",phone="8 (000) 000-00-00",legal_topic="Тестовая тема",
                      description="Только синтетические данные")
        with ThreadPoolExecutor(max_workers=4) as pool:
            drafts = list(pool.map(lambda _: tools.prepare(**values,expected_version=snapshot.version),range(4)))
        assert len({draft.request_id for draft in drafts}) == 1
        draft = drafts[0]
        args = {**values,"session_id":a.session_id,"consent":True}
        assert tools.create_lead(**args,request_id=draft.request_id).status == LeadStatus.CONSENT_REQUIRED
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "0"
        refused = tools.record_consent(draft.request_id,consent=False,expected_version=draft.session_version)
        assert tools.create_lead(**args,request_id=draft.request_id).status == LeadStatus.CONSENT_REQUIRED
        try:
            tools.record_consent(draft.request_id,consent=True,expected_version=draft.session_version)
            raise AssertionError("Stale consent event accepted")
        except ConflictError:
            pass
        accepted = tools.record_consent(draft.request_id,consent=True,expected_version=refused.session_version)
        assert accepted.consent_at is not None
        revoked = tools.record_consent(draft.request_id,consent=False,expected_version=accepted.session_version)
        assert tools.create_lead(**args,request_id=draft.request_id).status == LeadStatus.CONSENT_REQUIRED
        accepted = tools.record_consent(draft.request_id,consent=True,expected_version=revoked.session_version)
        changed = tools.create_lead(**{**args,"description":"Подмена моделью"},request_id=draft.request_id)
        assert changed.status == LeadStatus.CONFLICT
        foreign = tools.create_lead(**{**args,"session_id":b.session_id},request_id=draft.request_id)
        assert foreign.status == LeadStatus.SESSION_UNAVAILABLE
        forged = LeadTools(repo,SessionCredentials(a.session_id,b.token),"synthetic-lead-consent")
        assert forged.create_lead(**args,request_id=draft.request_id).status == LeadStatus.SESSION_UNAVAILABLE

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: tools.create_lead(**args,request_id=draft.request_id),range(6)))
        assert all(result.success for result in results)
        assert sum(result.status == LeadStatus.CREATED for result in results) == 1
        assert len({result.lead_id for result in results}) == 1
        assert all(result.notification_status == "pending" for result in results)
        lead_id = results[0].lead_id
        counts = execute_sql(f"select (select count(*) from public.leads where session_id='{a.session_id}'),"
                             f"(select count(*) from public.notification_logs where lead_id='{lead_id}');")
        assert counts == "1|1"
        # После нового клиента и объекта tool request_id и согласие остаются в БД.
        with SupabaseClient(settings,make_transport(execute_sql)) as restarted_client:
            restored = LeadTools(Repositories(restarted_client),a,"synthetic-lead-consent")
            saved = restored.get_request(draft.request_id)
            assert saved.lead_id == lead_id and saved.consent_at == accepted.consent_at
            assert restored.create_lead(**args,request_id=draft.request_id).status == LeadStatus.ALREADY_EXISTS

        # Отказ уведомления не отменяет заявку; tool читает реальный статус БД.
        current_request = tools.get_request(draft.request_id)
        tools.record_consent(draft.request_id,consent=True,expected_version=current_request.session_version)
        assert tools.create_lead(**args,request_id=draft.request_id).status == LeadStatus.ALREADY_EXISTS
        original_date_preserved = execute_sql(
            f"select consent_at = '{accepted.consent_at}'::timestamptz from public.leads where id='{lead_id}';")
        assert original_date_preserved == "t"
        execute_sql(f"update public.notification_logs set status='failed' where lead_id='{lead_id}';")
        saved = tools.create_lead(**args,request_id=draft.request_id)
        assert saved.success and saved.notification_status == "failed" and saved.lead_id == lead_id
        current = memory.load(a)
        new_values = {**values,"description":"Изменённое синтетическое описание"}
        changed_draft = tools.prepare(**new_values,expected_version=current.version)
        assert changed_draft.request_id != draft.request_id and changed_draft.consent_at is None
        assert tools.create_lead(**args,request_id=draft.request_id).status == LeadStatus.SESSION_UNAVAILABLE
        assert tools.create_lead(**{**args,**new_values},request_id=changed_draft.request_id).status == LeadStatus.CONSENT_REQUIRED
        memory.clear_history(a)
        assert tools.create_lead(**{**args,**new_values},request_id=changed_draft.request_id).status == LeadStatus.SESSION_UNAVAILABLE
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "1"
        assert repo.delete_leads(a)["deleted_leads"] == 1
        assert execute_sql(f"select count(*) from public.notification_logs where lead_id='{lead_id}';") == "0"

        # Новая заявка: ошибка создания outbox откатывает и запись заявки, и lead_id черновика.
        snapshot = memory.initialize(a,history_consent=True,notice_version="synthetic-history-notice")
        draft = tools.prepare(**values,expected_version=snapshot.version)
        tools.record_consent(draft.request_id,consent=True,expected_version=draft.session_version)
        execute_sql("""create function public.reject_tools_outbox() returns trigger language plpgsql as $$
            begin raise exception 'synthetic outbox failure'; end $$;
            create trigger reject_tools_outbox before insert on public.notification_logs
            for each row execute function public.reject_tools_outbox();""")
        failed = tools.create_lead(**args,request_id=draft.request_id)
        assert failed.status == LeadStatus.UNCONFIRMED and failed.lead_id is None
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "0"
        assert tools.get_request(draft.request_id).lead_id is None
        execute_sql("drop trigger reject_tools_outbox on public.notification_logs; drop function public.reject_tools_outbox();")
        assert tools.create_lead(**args,request_id=draft.request_id).success
        # Проверка прав новой таблицы и RPC обеими браузерными ролями через tools_health.
        assert repo.tools_health()["rpc_ready"] is True
        memory.delete_session(a)
        memory.delete_session(b)
    print("PASS: Tool consent is bound to payload; forged consent/session, changed draft and stale consent rejected")
    print("PASS: six concurrent tool calls create one lead/outbox; restart, failed delivery, clear/delete and outbox rollback")
```

## tests/memory_postgres_checks.py

```python
"""Memory → репозитории → SQL через тестовый HTTP-мост, без облачного PostgREST."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import re
from uuid import uuid4

import httpx

from config import SupabaseSettings
from core.memory import Memory
from core.router import BookingStage, Intent
from database.repositories import Repositories, SessionCredentials
from database.supabase_client import ConflictError, DatabaseError, SessionAccessError, SupabaseClient


def make_transport(execute_sql) -> httpx.MockTransport:
    """Только тестовый мост к отдельной временной БД из check_schema_local.py."""
    def literal(value):
        if value is None:
            return "null"
        if type(value) is int:
            return str(value)
        if type(value) is bool:
            return "true" if value else "false"
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        if not isinstance(value, str):
            raise ValueError("Unsupported SQL test argument")
        return "'" + value.replace("'", "''") + "'"

    def bridge(request):
        function = request.url.path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"legal_ai_[a-z_]+", function):
            raise ValueError("Invalid test function name")
        parameters = json.loads(request.content)
        if not all(re.fullmatch(r"p_[a-z_]+", k) for k in parameters):
            raise ValueError("Invalid test parameter")
        arguments = ",".join(k + " => " + literal(v) for k, v in parameters.items())
        sql = f"set role service_role; select public.{function}({arguments})::text;"
        try:
            data = json.loads(execute_sql(sql))
        except RuntimeError as exc:
            match = re.search(r"\b(P0002|40001|23505|22023)\b", str(exc))
            return httpx.Response(409 if match else 500,
                                  json={"code": match.group(0) if match else "test_sql_failure"})
        return httpx.Response(200, json=data)

    return httpx.MockTransport(bridge)


def run_checks(execute_sql) -> None:
    settings = SupabaseSettings("https://local-test.invalid", "sb_secret_" + "x" * 32)
    transport = make_transport(execute_sql)
    with SupabaseClient(settings, transport) as client:
        repositories = Repositories(client)
        repositories.health()
        repositories.memory_health()
        memory = Memory(repositories)
        a, b = repositories.new_credentials(), repositories.new_credentials()
        first = memory.initialize(a, history_consent=True, notice_version="synthetic-test-notice-v1")
        other = memory.initialize(b, history_consent=True, notice_version="synthetic-test-notice-v1")
        user_id, assistant_id = str(uuid4()), str(uuid4())
        first = memory.append(a, message_id=user_id, role="user", content="Вопрос по алиментам",
                              expected_version=first.version)
        state = replace(first.state, legal_topic="Взыскание алиментов", client_name="Синтетическое имя",
                        facts=("Есть решение суда",), booking_stage=BookingStage.NAME)
        first = memory.save_state(a, expected_version=first.version, state=state)
        previous_version = first.version
        first = memory.append(a, message_id=assistant_id, role="assistant", content="Как вас зовут?",
                              expected_version=previous_version)
        repeat = memory.append(a, message_id=assistant_id, role="assistant", content="Как вас зовут?",
                               expected_version=previous_version)
        assert repeat.version == first.version and len(repeat.messages) == 2
        other = memory.append(b, message_id=str(uuid4()), role="user", content="Вопрос о товаре",
                              expected_version=other.version)
        forged = SessionCredentials(a.session_id, b.token)
        try:
            memory.load(forged)
            raise AssertionError("Another session token was accepted")
        except SessionAccessError:
            pass

        # Новый клиент и новый объект Memory: контекст читается из PostgreSQL, не из кэша.
        with SupabaseClient(settings, make_transport(execute_sql)) as restarted_client:
            restored_memory = Memory(Repositories(restarted_client))
            restored = restored_memory.load(a)
            assert restored == first
            assert restored.state.facts == ("Есть решение суда",)
            assert restored.state.client_name == "Синтетическое имя"
            assert restored_memory.load(b) == other
            route, _ = restored_memory.route(a, "Тестовое имя")
            assert route.intent == Intent.BOOKING and route.legal_topic == "Взыскание алиментов"
            short = Memory(Repositories(restarted_client), history_limit=1).load(a)
            assert len(short.messages) == 1 and short.state.facts == restored.state.facts

        def simultaneous(index):
            try:
                memory.append(a, message_id=str(uuid4()), role="user", content=f"Тест {index}",
                              expected_version=first.version)
                return "saved"
            except ConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(simultaneous, range(2)))
        assert sorted(outcomes) == ["conflict", "saved"]
        assert memory.clear_history(a) == 3
        cleared = memory.load(a)
        assert not cleared.messages and not cleared.state.history_allowed
        assert cleared.state.client_name is None and cleared.state.booking_stage == BookingStage.IDLE
        try:
            memory.append(a, message_id=user_id, role="user", content="Вопрос по алиментам",
                          expected_version=first.version)
            raise AssertionError("Stale append restored cleared history")
        except DatabaseError:
            pass
        memory.initialize(a, history_consent=True, notice_version="synthetic-test-notice-v1")
        try:
            memory.append(a, message_id=user_id, role="user", content="Вопрос по алиментам",
                          expected_version=first.version)
            raise AssertionError("Stale append succeeded after reconsent")
        except ConflictError:
            pass
        assert not memory.load(a).messages
        assert memory.load(b) == other
        memory.delete_session(a)
        try:
            memory.load(a)
            raise AssertionError("Deleted memory remained accessible")
        except SessionAccessError:
            pass
        assert memory.load(b) == other
        memory.delete_session(b)
    print("PASS: Memory restores DB context in new client, separates users, limits history and routes by saved state")
    print("PASS: Memory consent, idempotent retries, concurrent version conflict, clear without stale resurrection, deletion")
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
