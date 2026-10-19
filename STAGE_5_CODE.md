# Полный код этапа 5

Снимок исходных файлов для просмотра. Рабочие версии находятся по указанным путям. Секретов в этом документе нет.

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

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


class ConfigError(ValueError):
    pass


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
```

## database/schema.sql

```sql
-- Этап 5, схема v1. Только новый выделенный проект/БД. Выполнить от postgres.
-- Не миграция существующих таблиц. Ошибка откатывает весь скрипт.
begin;

create table public.chat_sessions (
    id uuid primary key,
    token_hash text not null check (token_hash ~ '^[0-9a-f]{64}$'),
    state jsonb not null default '{}'::jsonb
        check (jsonb_typeof(state) = 'object' and octet_length(state::text) <= 8000),
    version bigint not null default 0 check (version >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.chat_messages (
    id uuid primary key,
    session_id uuid not null references public.chat_sessions(id) on delete cascade,
    sequence bigint generated always as identity unique,
    role text not null check (role in ('user', 'assistant')),
    content text not null check (char_length(btrim(content)) between 1 and 16000),
    created_at timestamptz not null default now()
);
create index chat_messages_session_order on public.chat_messages(session_id, sequence desc);

create table public.leads (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(btrim(name)) between 1 and 100),
    phone text not null check (phone ~ '^\+7[0-9]{10}$'),
    legal_topic text not null check (char_length(btrim(legal_topic)) between 1 and 200),
    description text not null check (char_length(btrim(description)) between 1 and 2000),
    status text not null default 'new' check (status in ('new', 'contacted', 'closed')),
    consent_at timestamptz not null default now(),
    consent_version text not null check (char_length(btrim(consent_version)) between 1 and 100),
    session_id uuid not null references public.chat_sessions(id) on delete cascade,
    idempotency_key uuid not null,
    created_at timestamptz not null default now(),
    unique (session_id, idempotency_key)
);

create table public.notification_logs (
    id uuid primary key default gen_random_uuid(),
    lead_id uuid not null unique references public.leads(id) on delete cascade,
    status text not null default 'pending' check (status in ('pending', 'sending', 'sent', 'failed')),
    error_message text check (char_length(error_message) <= 500),
    attempts integer not null default 0 check (attempts >= 0),
    next_attempt_at timestamptz not null default now(),
    locked_until timestamptz,
    sent_at timestamptz,
    created_at timestamptz not null default now()
);
create index notification_logs_pending on public.notification_logs(next_attempt_at)
    where status in ('pending', 'failed');

alter table public.chat_sessions enable row level security;
alter table public.chat_messages enable row level security;
alter table public.leads enable row level security;
alter table public.notification_logs enable row level security;
-- Нет разрешающих политик для браузерных ролей: доступ только с сервера.
revoke all on public.chat_sessions, public.chat_messages, public.leads,
    public.notification_logs from public, anon, authenticated;
revoke all on sequence public.chat_messages_sequence_seq from public, anon, authenticated;
grant usage on schema public to service_role;
grant select, insert, update, delete on public.chat_sessions, public.chat_messages,
    public.leads, public.notification_logs to service_role;
grant usage, select on sequence public.chat_messages_sequence_seq to service_role;

-- SECURITY INVOKER: функции не повышают права вызывающего.
-- Функции с префиксом legal_ai_ доступны только service_role.
create function public.legal_ai_lock_session(p_session_id uuid, p_token_hash text)
returns public.chat_sessions language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
begin
    select * into s from public.chat_sessions
        where id = p_session_id and token_hash = p_token_hash for update;
    if not found then
        raise exception 'session unavailable' using errcode = 'P0002';
    end if;
    return s;
end;
$$;

create function public.legal_ai_get_session(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    return to_jsonb(s) - 'token_hash';
end;
$$;

create function public.legal_ai_create_session(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
begin
    insert into public.chat_sessions(id, token_hash) values (p_session_id, p_token_hash)
        on conflict (id) do nothing;
    return public.legal_ai_get_session(p_session_id, p_token_hash);
end;
$$;

create function public.legal_ai_append_message(
    p_session_id uuid, p_token_hash text, p_message_id uuid, p_role text, p_content text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare m public.chat_messages;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    insert into public.chat_messages(id, session_id, role, content)
        values (p_message_id, p_session_id, p_role, p_content)
        on conflict (id) do nothing returning * into m;
    if not found then
        select * into m from public.chat_messages where id = p_message_id;
        if m.session_id is distinct from p_session_id or m.role is distinct from p_role
            or m.content is distinct from p_content then
            raise exception 'idempotency conflict' using errcode = '23505';
        end if;
    else
        update public.chat_sessions set updated_at = now(), version = version + 1
            where id = p_session_id;
    end if;
    return to_jsonb(m);
end;
$$;

create function public.legal_ai_messages(p_session_id uuid, p_token_hash text, p_limit integer default 40)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare result jsonb;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    if p_limit is null or p_limit not between 1 and 100 then
        raise exception 'invalid history limit' using errcode = '22023';
    end if;
    select coalesce(jsonb_agg(to_jsonb(m) order by m.sequence), '[]'::jsonb) into result
        from (select * from public.chat_messages where session_id = p_session_id
              order by sequence desc limit p_limit) m;
    return result;
end;
$$;

create function public.legal_ai_update_state(
    p_session_id uuid, p_token_hash text, p_expected_version bigint, p_state jsonb)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    if s.version is distinct from p_expected_version then
        raise exception 'state conflict' using errcode = '40001';
    end if;
    update public.chat_sessions set state = p_state, version = version + 1, updated_at = now()
        where id = p_session_id returning * into s;
    return to_jsonb(s) - 'token_hash';
end;
$$;

create function public.legal_ai_create_lead(
    p_session_id uuid, p_token_hash text, p_idempotency_key uuid,
    p_name text, p_phone text, p_legal_topic text, p_description text,
    p_consent boolean, p_consent_version text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare l public.leads;
declare duplicate boolean := false;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    if p_consent is distinct from true then
        raise exception 'explicit consent required' using errcode = '22023';
    end if;
    select * into l from public.leads
        where session_id = p_session_id and idempotency_key = p_idempotency_key;
    if found then
        duplicate := true;
        if l.name is distinct from p_name or l.phone is distinct from p_phone
            or l.legal_topic is distinct from p_legal_topic or l.description is distinct from p_description
            or l.consent_version is distinct from p_consent_version then
            raise exception 'idempotency conflict' using errcode = '23505';
        end if;
    else
        insert into public.leads(session_id, idempotency_key, name, phone, legal_topic,
            description, consent_version)
            values (p_session_id, p_idempotency_key, p_name, p_phone, p_legal_topic,
                    p_description, p_consent_version) returning * into l;
        -- Заявка и задача уведомления фиксируются одной транзакцией RPC.
        insert into public.notification_logs(lead_id) values (l.id);
    end if;
    return jsonb_build_object('id', l.id, 'created_at', l.created_at, 'duplicate', duplicate);
end;
$$;

create function public.legal_ai_clear_history(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.chat_messages where session_id = p_session_id;
    get diagnostics n = row_count;
    update public.chat_sessions set state = '{}'::jsonb, version = version + 1, updated_at = now()
        where id = p_session_id;
    return jsonb_build_object('deleted_messages', n);
end;
$$;

create function public.legal_ai_delete_leads(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n integer;
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.leads where session_id = p_session_id;
    get diagnostics n = row_count;
    return jsonb_build_object('deleted_leads', n);
end;
$$;

create function public.legal_ai_delete_session(p_session_id uuid, p_token_hash text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
begin
    perform public.legal_ai_lock_session(p_session_id, p_token_hash);
    delete from public.chat_sessions where id = p_session_id;
    return jsonb_build_object('deleted', true);
end;
$$;

create function public.legal_ai_health()
returns jsonb language sql security invoker set search_path = '' as $$
    select jsonb_build_object(
        'schema_version', 1,
        'rls_enabled', (select count(*) = 4 and bool_and(c.relrowsecurity)
            from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public' and c.relname in
                ('chat_sessions', 'chat_messages', 'leads', 'notification_logs')),
        'public_access', (
            exists (select 1 from (values ('chat_sessions'), ('chat_messages'),
                ('leads'), ('notification_logs')) t(name)
                cross join (values ('anon'), ('authenticated')) r(name)
                cross join (values ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) p(name)
                where pg_catalog.has_table_privilege(r.name, 'public.' || t.name, p.name))
            or exists (select 1 from pg_catalog.pg_proc p
                join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                where n.nspname = 'public' and p.proname like 'legal_ai_%'
                and (pg_catalog.has_function_privilege('anon', p.oid, 'EXECUTE')
                     or pg_catalog.has_function_privilege('authenticated', p.oid, 'EXECUTE')))
        )
    );
$$;

-- Default EXECUTE PUBLIC необходимо отозвать у каждой новой функции.
do $$
declare f record;
begin
    for f in select p.oid::regprocedure as signature from pg_catalog.pg_proc p
        join pg_catalog.pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname like 'legal_ai_%'
    loop
        execute format('revoke all on function %s from public, anon, authenticated', f.signature);
        execute format('grant execute on function %s to service_role', f.signature);
    end loop;
end;
$$;

notify pgrst, 'reload schema';
commit;
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


class SupabaseClient:
    def __init__(self, settings: SupabaseSettings, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._http = httpx.Client(timeout=settings.timeout, transport=transport,
                                  follow_redirects=False)

    def rpc(self, name: str, parameters: dict) -> object:
        allowed = {"health", "create_session", "get_session", "append_message", "messages",
                   "update_state", "create_lead", "clear_history", "delete_leads", "delete_session"}
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
```

## app.py

```python
"""Проверка структуры, LLM, Router и Supabase (этапы 1–5)."""

import argparse
from pathlib import Path
import sys


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Требуется Python 3.10 или новее.")
    parser = argparse.ArgumentParser(description="AI-консультант юриста: этап 5")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-llm", action="store_true", help="Один реальный тестовый запрос к LLM API")
    mode.add_argument("--check-router", action="store_true", help="Примеры маршрутизации без сети")
    mode.add_argument("--route", metavar="TEXT", help="Определить намерение без сети")
    mode.add_argument("--check-db", action="store_true", help="Проверить Supabase и ограничения схемы без записи данных")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    required = ("knowledge", "core", "llm", "tools", "database", "ui", "tests")
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise SystemExit("Отсутствуют каталоги: " + ", ".join(missing))
    if args.check_db:
        try:
            from config import ConfigError, load_supabase_settings
            from database.repositories import Repositories
            from database.supabase_client import DatabaseError, SupabaseClient
        except ImportError:
            raise SystemExit("Установите зависимости: python -m pip install -r requirements.txt") from None
        try:
            with SupabaseClient(load_supabase_settings()) as client:
                Repositories(client).health()
            print("Supabase ответил. Схема v1: RLS включён, доступ anon/authenticated закрыт.")
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
        print("Этап 5. Router: --check-router; LLM: --check-llm; Supabase: --check-db.")
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
# БД: серверный Secret key (sb_secret_...), не publishable/anon.
SUPABASE_URL=
SUPABASE_KEY=
SUPABASE_TIMEOUT_SECONDS=15
# Будущий этап Telegram.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

## tests/test_database.py

```python
"""HTTP-контракт Supabase и валидация серверной границы без доступа к облаку."""

import base64
import hashlib
import json
import os
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import httpx

from config import ConfigError, SupabaseSettings, load_supabase_settings
from database.repositories import Repositories, SessionCredentials
from database.supabase_client import ConflictError, DatabaseError, SessionAccessError, SupabaseClient


KEY = "sb_secret_" + "x" * 32


class DatabaseTests(TestCase):
    def setUp(self):
        self.settings = SupabaseSettings("https://example.supabase.co", KEY)
        self.credentials = SessionCredentials(str(uuid4()), "a" * 43)

    def client(self, handler):
        return SupabaseClient(self.settings, httpx.MockTransport(handler))

    def test_secret_key_headers_and_readonly_health(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://example.supabase.co/rest/v1/rpc/legal_ai_health")
            self.assertEqual(request.headers["apikey"], KEY)
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(json.loads(request.content), {})
            return httpx.Response(200, json={"schema_version": 1, "rls_enabled": True, "public_access": False})
        with self.client(handler) as client:
            self.assertEqual(Repositories(client).health()["schema_version"], 1)
        self.assertNotIn(KEY, repr(self.settings))

    def test_legacy_service_role_header(self):
        payload = base64.urlsafe_b64encode(b'{"role":"service_role"}').decode().rstrip("=")
        key = "eyJhbGciOiJIUzI1NiJ9." + payload + ".signature"
        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer " + key)
            return httpx.Response(200, json={})
        with SupabaseClient(SupabaseSettings(self.settings.url, key), httpx.MockTransport(handler)) as client:
            client.rpc("health", {})

    def test_config_rejects_public_and_bad_keys(self):
        anon = base64.urlsafe_b64encode(b'{"role":"anon"}').decode().rstrip("=")
        for key in ("", "sb_publishable_" + "x" * 32, "not-a-key" * 4, "header." + anon + ".sig"):
            with self.subTest(key_type=key[:8]), self.assertRaises(ConfigError):
                SupabaseSettings(self.settings.url, key)

    def test_config_rejects_unsafe_urls(self):
        for url in ("", "http://example.com", "https://user:pw@example.com", "https://example.com/path",
                    "https://example.com?x=1", "https://[invalid"):
            with self.assertRaises(ConfigError):
                SupabaseSettings(url, KEY)

    def test_config_independent_of_llm(self):
        with patch.dict(os.environ, {"SUPABASE_URL": self.settings.url, "SUPABASE_KEY": KEY}, clear=True), \
                patch("config.load_dotenv"):
            self.assertEqual(load_supabase_settings().url, self.settings.url)

    def test_credential_secrets_hashed_and_masked(self):
        parameters = self.credentials.parameters()
        self.assertEqual(parameters["p_token_hash"], hashlib.sha256(b"a" * 43).hexdigest())
        self.assertNotIn(self.credentials.token, repr(self.credentials))
        self.assertNotIn(self.credentials.token, json.dumps(parameters))
        with self.client(lambda r: self.fail("No HTTP expected")) as client:
            a = Repositories(client).new_credentials()
            b = Repositories(client).new_credentials()
            self.assertNotEqual(a, b)
            self.assertEqual(len(a.token), 43)

    def test_session_and_messages_scoped_by_credentials(self):
        def handler(request):
            params = json.loads(request.content)
            self.assertEqual(params["p_session_id"], self.credentials.session_id)
            self.assertEqual(params["p_token_hash"], self.credentials.parameters()["p_token_hash"])
            if request.url.path.endswith("messages"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"id": self.credentials.session_id, "state": {}, "version": 0})
        with self.client(handler) as client:
            repo = Repositories(client)
            repo.create_session(self.credentials)
            repo.get_session(self.credentials)
            self.assertEqual(repo.messages(self.credentials), [])

    def test_safe_errors_and_no_retry(self):
        for status, code, exception in ((403, "42501", DatabaseError), (401, "auth", DatabaseError),
                                      (404, "PGRST202", DatabaseError), (400, "P0002", SessionAccessError),
                                      (409, "23505", ConflictError), (409, "40001", ConflictError),
                                      (429, "rate", DatabaseError), (500, "error", DatabaseError),
                                      (302, "redirect", DatabaseError)):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={"code": code, "message": KEY + " private phone"})
            with self.subTest(status=status, code=code), self.client(handler) as client:
                with self.assertRaises(exception) as error:
                    client.rpc("health", {})
                self.assertNotIn(KEY, str(error.exception))
                self.assertNotIn("private phone", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_network_and_bad_json(self):
        for kind in (httpx.ReadTimeout, httpx.ConnectError):
            def handler(request):
                raise kind(KEY, request=request)
            with self.client(handler) as client:
                with self.assertRaises(DatabaseError) as error:
                    client.rpc("health", {})
                self.assertNotIn(KEY, str(error.exception))
                self.assertIsNone(error.exception.__context__)
        with self.client(lambda r: httpx.Response(200, text="invalid")) as client:
            with self.assertRaises(DatabaseError):
                client.rpc("health", {})

    def test_unsafe_schema_health_rejected(self):
        for result in ([], {}, {"schema_version": 1, "rls_enabled": False, "public_access": False},
                       {"schema_version": 1, "rls_enabled": True, "public_access": True}):
            with self.client(lambda r: httpx.Response(200, json=result)) as client:
                with self.assertRaises(DatabaseError):
                    Repositories(client).health()

    def test_invalid_input_never_reaches_http(self):
        with self.client(lambda r: self.fail("No HTTP expected")) as client:
            repo = Repositories(client)
            for role, content in (("system", "rules"), ("user", " "), ("assistant", "x" * 16001)):
                with self.assertRaises(ValueError):
                    repo.append_message(self.credentials, str(uuid4()), role, content)
            for limit in (0, 101, True):
                with self.assertRaises(ValueError):
                    repo.messages(self.credentials, limit)
            for state in ({"x": float("nan")}, {"x": "x" * 8001}, {"x": object()}):
                with self.assertRaises(ValueError):
                    repo.update_state(self.credentials, 0, state)
            with self.assertRaises(ValueError):
                client.rpc("../leads", {})
            with self.assertRaises(ValueError):
                repo.get_session(SessionCredentials(self.credentials.session_id, "short"))

    def test_lead_consent_and_phone_validation(self):
        with self.client(lambda r: self.fail("No HTTP expected")) as client:
            for consent, phone in ((False, "+70000000000"), (1, "+70000000000"), (True, "bad")):
                with self.assertRaises(ValueError):
                    Repositories(client).create_lead(self.credentials, idempotency_key=str(uuid4()),
                        name="Тест", phone=phone, legal_topic="Тест", description="Синтетический тест",
                        consent=consent, consent_version="test-v1")

    def test_lead_only_confirmed_by_returned_id(self):
        key = str(uuid4())
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["p_idempotency_key"], key)
            self.assertTrue(payload["p_consent"])
            return httpx.Response(200, json={"id": key, "created_at": "2026-10-16T10:00:00Z", "duplicate": False})
        with self.client(handler) as client:
            result = Repositories(client).create_lead(self.credentials, idempotency_key=key,
                name="Тест", phone="+70000000000", legal_topic="Тест", description="Синтетический тест",
                consent=True, consent_version="test-v1")
            self.assertEqual(result["id"], key)
        with self.client(lambda r: httpx.Response(200, json={})) as client:
            with self.assertRaises(DatabaseError):
                Repositories(client).create_lead(self.credentials, idempotency_key=key,
                    name="Тест", phone="+70000000000", legal_topic="Тест", description="Тест",
                    consent=True, consent_version="test-v1")

    def test_deletions_are_scoped_and_confirmed(self):
        def handler(request):
            self.assertEqual(json.loads(request.content), self.credentials.parameters())
            if request.url.path.endswith("clear_history"):
                result = {"deleted_messages": 2}
            elif request.url.path.endswith("delete_leads"):
                result = {"deleted_leads": 1}
            else:
                result = {"deleted": True}
            return httpx.Response(200, json=result)
        with self.client(handler) as client:
            repo = Repositories(client)
            self.assertEqual(repo.clear_history(self.credentials)["deleted_messages"], 2)
            self.assertEqual(repo.delete_leads(self.credentials)["deleted_leads"], 1)
            self.assertTrue(repo.delete_session(self.credentials)["deleted"])
```

## tests/schema_checks.sql

```sql
-- Выполняется только во временной локальной БД через check_schema_local.py.
set role service_role;
do $$
declare a uuid := '00000000-0000-0000-0000-000000000001';
declare b uuid := '00000000-0000-0000-0000-000000000002';
declare h text := repeat('a',64);
declare j jsonb;
declare first_id text;
begin
    j := public.legal_ai_health();
    assert j = '{"schema_version":1,"rls_enabled":true,"public_access":false}'::jsonb;
    j := public.legal_ai_create_session(a,h);
    assert j->>'id' = a::text;
    assert not j ? 'token_hash';
    perform public.legal_ai_create_session(a,h);
    perform public.legal_ai_create_session(b,repeat('b',64));
    begin
        perform public.legal_ai_get_session(a,repeat('b',64));
        raise exception 'wrong session secret was accepted';
    exception when no_data_found then null;
    end;
    perform public.legal_ai_append_message(a,h,
        '00000000-0000-0000-0000-000000000010','user','Тестовый вопрос');
    perform public.legal_ai_append_message(a,h,
        '00000000-0000-0000-0000-000000000010','user','Тестовый вопрос');
    perform public.legal_ai_append_message(a,h,
        '00000000-0000-0000-0000-000000000011','assistant','Тестовый ответ');
    j := public.legal_ai_messages(a,h,40);
    assert jsonb_array_length(j) = 2;
    assert j->0->>'role' = 'user';
    assert j->1->>'role' = 'assistant';
    assert jsonb_array_length(public.legal_ai_messages(a,h,1)) = 1;
    assert jsonb_array_length(public.legal_ai_messages(b,repeat('b',64),40)) = 0;
    begin
        perform public.legal_ai_append_message(a,h,
            '00000000-0000-0000-0000-000000000010','user','Другой текст');
        raise exception 'message id conflict was accepted';
    exception when unique_violation then null;
    end;
    begin
        perform public.legal_ai_append_message(b,repeat('b',64),
            '00000000-0000-0000-0000-000000000010','user','Тестовый вопрос');
        raise exception 'cross-session message reuse was accepted';
    exception when unique_violation then null;
    end;
    j := public.legal_ai_get_session(a,h);
    j := public.legal_ai_update_state(a,h,(j->>'version')::bigint,'{"booking_stage":"name"}');
    assert j->'state'->>'booking_stage' = 'name';
    begin
        perform public.legal_ai_update_state(a,h,0,'{}');
        raise exception 'stale state version was accepted';
    exception when serialization_failure then null;
    end;
    begin
        perform public.legal_ai_create_lead(a,h,'00000000-0000-0000-0000-000000000020',
            'Тест','+70000000000','Тестовая тема','Синтетическое описание',false,'test-v1');
        raise exception 'missing consent was accepted';
    exception when invalid_parameter_value then null;
    end;
    j := public.legal_ai_create_lead(a,h,'00000000-0000-0000-0000-000000000020',
        'Тест','+70000000000','Тестовая тема','Синтетическое описание',true,'test-v1');
    assert (j->>'duplicate')::boolean = false;
    first_id := j->>'id';
    j := public.legal_ai_create_lead(a,h,'00000000-0000-0000-0000-000000000020',
        'Тест','+70000000000','Тестовая тема','Синтетическое описание',true,'test-v1');
    assert (j->>'duplicate')::boolean = true;
    assert j->>'id' = first_id;
    assert (select count(*) from public.notification_logs where lead_id = first_id::uuid) = 1;
    begin
        perform public.legal_ai_create_lead(a,h,'00000000-0000-0000-0000-000000000020',
            'Другой','+70000000000','Тестовая тема','Синтетическое описание',true,'test-v1');
        raise exception 'lead id conflict was accepted';
    exception when unique_violation then null;
    end;
    j := public.legal_ai_clear_history(a,h);
    assert (j->>'deleted_messages')::int = 2;
    assert jsonb_array_length(public.legal_ai_messages(a,h,40)) = 0;
    assert (public.legal_ai_get_session(a,h))->'state' = '{}'::jsonb;
    assert (select count(*) from public.leads where session_id = a) = 1;
    j := public.legal_ai_delete_leads(a,h);
    assert (j->>'deleted_leads')::int = 1;
    assert (select count(*) from public.notification_logs) = 0;
end;
$$;
reset role;

-- Проверка атомарности: ошибка outbox должна откатить и вставку заявки.
create function public.reject_test_outbox() returns trigger language plpgsql as $$
begin raise exception 'simulated outbox failure'; end;
$$;
create trigger reject_outbox before insert on public.notification_logs
    for each row execute function public.reject_test_outbox();
set role service_role;
do $$
begin
    begin
        perform public.legal_ai_create_lead('00000000-0000-0000-0000-000000000001',repeat('a',64),
            '00000000-0000-0000-0000-000000000030',
            'Тест','+70000000000','Тестовая тема','Синтетическое описание',true,'test-v1');
        raise exception 'outbox failure not propagated';
    exception when raise_exception then
        if sqlerrm <> 'simulated outbox failure' then raise; end if;
    end;
    assert not exists (select 1 from public.leads
        where idempotency_key = '00000000-0000-0000-0000-000000000030');
end;
$$;
reset role;
drop trigger reject_outbox on public.notification_logs;
drop function public.reject_test_outbox();

-- Прямое чтение и вызов RPC запрещены обеим браузерным ролям.
set role anon;
do $$
begin
    begin
        perform * from public.leads;
        raise exception 'anon could read leads';
    exception when insufficient_privilege then null;
    end;
    begin
        perform public.legal_ai_health();
        raise exception 'anon could call RPC';
    exception when insufficient_privilege then null;
    end;
end;
$$;
reset role;
set role authenticated;
do $$
begin
    begin
        perform * from public.chat_messages;
        raise exception 'authenticated could read history';
    exception when insufficient_privilege then null;
    end;
    begin
        perform public.legal_ai_get_session('00000000-0000-0000-0000-000000000001',repeat('a',64));
        raise exception 'authenticated could call RPC';
    exception when insufficient_privilege then null;
    end;
end;
$$;
reset role;
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
                                    creationflags=flags, timeout=60)
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
             "--locale=C", "--auth=scram-sha-256", "--pwfile", password_file])
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
