# Полный код этапа 6

Снимок исходных файлов для просмотра. Рабочие версии находятся по указанным путям. Действующих ключей и секретов сессий нет.

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

    def __post_init__(self) -> None:
        for value, label, maximum in ((self.legal_topic, "Тема", 200),
                                       (self.client_name, "Имя", 100),
                                       (self.history_notice_version, "Версия уведомления", 100)):
            if value is not None:
                text_value(value, label, maximum)
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
        return {"memory_schema": 1, "legal_topic": self.legal_topic, "facts": list(self.facts),
                "client_name": self.client_name, "booking_stage": self.booking_stage.value,
                "history_notice_version": self.history_notice_version,
                "history_consent_at": self.history_consent_at}

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationState":
        if not isinstance(data, dict):
            raise ValueError("Состояние Memory должно быть объектом.")
        if not data:
            return cls()
        required = {"memory_schema", "legal_topic", "facts", "client_name", "booking_stage",
                    "history_notice_version", "history_consent_at"}
        if set(data) != required or type(data["memory_schema"]) is not int or data["memory_schema"] != 1:
            raise ValueError("Неизвестный формат состояния Memory.")
        if not isinstance(data["facts"], list):
            raise ValueError("Некорректные обстоятельства Memory.")
        return cls(data["legal_topic"], tuple(data["facts"]), data["client_name"],
                   BookingStage(data["booking_stage"]), data["history_notice_version"],
                   data["history_consent_at"])


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

## database/migrations/002_memory.sql

```sql
-- Этап 6. Применить после schema.sql. Идемпотентная добавочная миграция.
begin;

create or replace function public.legal_ai_memory_snapshot(
    p_session_id uuid, p_token_hash text, p_limit integer default 40)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s jsonb;
begin
    -- Все существующие операции также берут блокировку этой строки.
    s := public.legal_ai_get_session(p_session_id, p_token_hash);
    return jsonb_build_object('session', s,
        'messages', public.legal_ai_messages(p_session_id, p_token_hash, p_limit));
end;
$$;

create or replace function public.legal_ai_memory_append(
    p_session_id uuid, p_token_hash text, p_message_id uuid,
    p_role text, p_content text, p_expected_version bigint, p_limit integer default 40)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare s public.chat_sessions;
declare m public.chat_messages;
begin
    s := public.legal_ai_lock_session(p_session_id, p_token_hash);
    -- Согласие задаёт сервер после явного действия пользователя, не модель.
    if nullif(btrim(s.state->>'history_notice_version'), '') is null
        or nullif(btrim(s.state->>'history_consent_at'), '') is null then
        raise exception 'history consent required' using errcode = '22023';
    end if;
    select * into m from public.chat_messages where id = p_message_id;
    if found then
        -- Повтор того же сообщения допустим даже после повышения версии.
        if m.session_id is distinct from p_session_id or m.role is distinct from p_role
            or m.content is distinct from p_content then
            raise exception 'message idempotency conflict' using errcode = '23505';
        end if;
    else
        if s.version is distinct from p_expected_version then
            raise exception 'stale memory version' using errcode = '40001';
        end if;
        perform public.legal_ai_append_message(
            p_session_id, p_token_hash, p_message_id, p_role, p_content);
    end if;
    return public.legal_ai_memory_snapshot(p_session_id, p_token_hash, p_limit);
end;
$$;

create or replace function public.legal_ai_memory_health()
returns jsonb language sql security invoker set search_path = '' as $$
    select jsonb_build_object('memory_version', 1, 'rpc_ready',
        (select count(*) = 3 and not bool_or(p.prosecdef)
            and not bool_or(pg_catalog.has_function_privilege('anon', p.oid, 'EXECUTE'))
            and not bool_or(pg_catalog.has_function_privilege('authenticated', p.oid, 'EXECUTE'))
         from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname in
            ('legal_ai_memory_snapshot', 'legal_ai_memory_append', 'legal_ai_memory_health')));
$$;

revoke all on function public.legal_ai_memory_snapshot(uuid,text,integer),
    public.legal_ai_memory_append(uuid,text,uuid,text,text,bigint,integer),
    public.legal_ai_memory_health() from public, anon, authenticated;
grant execute on function public.legal_ai_memory_snapshot(uuid,text,integer),
    public.legal_ai_memory_append(uuid,text,uuid,text,text,bigint,integer),
    public.legal_ai_memory_health() to service_role;
notify pgrst, 'reload schema';
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
                   "update_state", "create_lead", "clear_history", "delete_leads", "delete_session",
                   "memory_snapshot", "memory_append", "memory_health"}
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

## app.py

```python
"""Проверка структуры, LLM, Router, Supabase и Memory (этапы 1–6)."""

import argparse
from pathlib import Path
import sys


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Требуется Python 3.10 или новее.")
    parser = argparse.ArgumentParser(description="AI-консультант юриста: этап 6")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-llm", action="store_true", help="Один реальный тестовый запрос к LLM API")
    mode.add_argument("--check-router", action="store_true", help="Примеры маршрутизации без сети")
    mode.add_argument("--route", metavar="TEXT", help="Определить намерение без сети")
    mode.add_argument("--check-db", action="store_true", help="Проверить Supabase и ограничения схемы без записи данных")
    mode.add_argument("--check-memory", action="store_true", help="Проверить Supabase и миграцию Memory без записи данных")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    required = ("knowledge", "core", "llm", "tools", "database", "ui", "tests")
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise SystemExit("Отсутствуют каталоги: " + ", ".join(missing))
    if args.check_db or args.check_memory:
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
                if args.check_memory:
                    repositories.memory_health()
            print("Supabase ответил. Схема v1: RLS включён, доступ anon/authenticated закрыт.")
            if args.check_memory:
                print("RPC Memory v1 доступны только серверной роли.")
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
        print("Этап 6. Router: --check-router; LLM: --check-llm; Supabase: --check-db; Memory: --check-memory.")
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

## tests/test_memory.py

```python
"""Проверка границы Memory. Постоянное хранение дополнительно проверяет PostgreSQL-runner."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from core.memory import ConversationState, HistoryConsentRequired, Memory, MemoryDataError
from core.router import BookingStage, Intent
from database.repositories import Repositories, SessionCredentials
from database.supabase_client import ConflictError, DatabaseError


class MemoryTests(TestCase):
    def setUp(self):
        self.credentials = SessionCredentials(str(uuid4()), "a" * 43)
        self.repo = Mock(spec=Repositories)
        self.memory = Memory(self.repo)
        self.state = ConversationState(
            legal_topic="Взыскание алиментов", facts=("Есть решение суда",), client_name="Тестовое имя",
            booking_stage=BookingStage.NAME, history_notice_version="test-notice-v1",
            history_consent_at="2026-10-16T10:00:00+00:00")
        self.data = {"session": {"id": self.credentials.session_id, "version": 2,
                                "state": self.state.to_dict()}, "messages": []}
        self.repo.memory_snapshot.return_value = deepcopy(self.data)

    def message_row(self, sequence=1, role="user", content="Хочу записаться"):
        return {"id": str(uuid4()), "session_id": self.credentials.session_id,
                "sequence": sequence, "role": role, "content": content}

    def test_load_restores_typed_state_and_history(self):
        self.data["messages"] = [self.message_row(), self.message_row(2, "assistant", "Как вас зовут?")]
        self.repo.memory_snapshot.return_value = self.data
        snapshot = self.memory.load(self.credentials)
        self.assertEqual(snapshot.state, self.state)
        self.assertEqual(snapshot.messages[-1].content, "Как вас зовут?")
        self.assertEqual(snapshot.version, 2)
        self.repo.memory_snapshot.assert_called_once_with(self.credentials, 40)

    def test_no_private_data_in_snapshot_repr(self):
        self.data["messages"] = [self.message_row(content="Частное сообщение")]
        self.repo.memory_snapshot.return_value = self.data
        snapshot = self.memory.load(self.credentials)
        self.assertNotIn("Частное сообщение", repr(snapshot))
        self.assertNotIn("Тестовое имя", repr(snapshot.state))
        self.assertNotIn(self.credentials.token, repr(snapshot))

    def test_refused_history_consent_never_calls_database(self):
        for consent in (False, None, 1, "да"):
            with self.assertRaises(HistoryConsentRequired):
                self.memory.initialize(self.credentials, history_consent=consent, notice_version="test-v1")
        self.assertEqual(self.repo.mock_calls, [])

    def test_initialize_existing_notice_preserves_timestamp(self):
        snapshot = self.memory.initialize(self.credentials, history_consent=True, notice_version="test-notice-v1")
        self.assertEqual(snapshot.state.history_consent_at, self.state.history_consent_at)
        self.repo.update_state.assert_not_called()

    def test_initialize_new_notice_keeps_existing_context(self):
        self.memory.initialize(self.credentials, history_consent=True, notice_version="test-notice-v2")
        _, version, state = self.repo.update_state.call_args.args
        self.assertEqual(version, 2)
        self.assertEqual(state["history_notice_version"], "test-notice-v2")
        self.assertEqual(state["facts"], list(self.state.facts))
        self.assertTrue(state["history_consent_at"].endswith("+00:00"))

    def test_empty_state_is_idle_and_without_consent(self):
        self.data["session"]["state"] = {}
        self.repo.memory_snapshot.return_value = self.data
        snapshot = self.memory.load(self.credentials)
        self.assertEqual(snapshot.state.booking_stage, BookingStage.IDLE)
        self.assertFalse(snapshot.state.history_allowed)

    def test_append_uses_message_id_version_and_credentials(self):
        self.repo.memory_append.return_value = self.data
        message_id = str(uuid4())
        self.memory.append(self.credentials, message_id=message_id, role="user", content="Тест", expected_version=2)
        self.repo.memory_append.assert_called_once_with(self.credentials, message_id=message_id,
            role="user", content="Тест", expected_version=2, limit=40)

    def test_save_state_uses_compare_and_swap(self):
        updated = replace(self.state, client_name="Другое тестовое имя", booking_stage=BookingStage.PHONE)
        self.memory.save_state(self.credentials, expected_version=2, state=updated)
        self.repo.update_state.assert_called_once_with(self.credentials, 2, updated.to_dict())

    def test_conflict_is_not_silently_retried(self):
        self.repo.update_state.side_effect = ConflictError("state conflict")
        with self.assertRaises(ConflictError):
            self.memory.save_state(self.credentials, expected_version=1, state=self.state)
        self.assertEqual(self.repo.update_state.call_count, 1)

    def test_save_state_cannot_grant_consent(self):
        changed = replace(self.state, history_notice_version="forged-version")
        with self.assertRaises(ValueError):
            self.memory.save_state(self.credentials, expected_version=2, state=changed)
        self.repo.update_state.assert_not_called()

    def test_save_without_history_consent_fails(self):
        self.data["session"]["state"] = {}
        self.repo.memory_snapshot.return_value = self.data
        with self.assertRaises(HistoryConsentRequired):
            self.memory.save_state(self.credentials, expected_version=0, state=self.state)
        self.repo.update_state.assert_not_called()

    def test_router_uses_persisted_stage_and_topic(self):
        result, snapshot = self.memory.route(self.credentials, "Тестовое имя")
        self.assertEqual(result.intent, Intent.BOOKING)
        self.assertEqual(result.legal_topic, "Взыскание алиментов")
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(self.memory.route(self.credentials, "Сколько стоит?")[0].intent, Intent.PRICE)

    def test_current_topic_overrides_saved_topic(self):
        result, _ = self.memory.route(self.credentials, "Как вернуть товар?")
        self.assertEqual(result.legal_topic, "Защита прав потребителей")

    def test_invalid_or_foreign_context_rejected(self):
        variants = []
        for changes in ({"id": str(uuid4())}, {"version": True}, {"version": -1},
                        {"token_hash": "private"}, {"state": {"booking_stage": "phone"}}):
            data = deepcopy(self.data)
            data["session"].update(changes)
            variants.append(data)
        for row in (self.message_row(role="system"), self.message_row(content=" ")):
            data = deepcopy(self.data)
            data["messages"] = [row]
            variants.append(data)
        data = deepcopy(self.data)
        row = self.message_row()
        row["session_id"] = str(uuid4())
        data["messages"] = [row]
        variants.append(data)
        variants.extend(({}, {"session": None, "messages": []}))
        for data in variants:
            self.repo.memory_snapshot.return_value = data
            with self.assertRaises(MemoryDataError):
                self.memory.load(self.credentials)

    def test_unordered_duplicate_and_oversized_history_rejected(self):
        row = self.message_row()
        for rows in ([self.message_row(2), self.message_row(1)], [row, row],
                     [self.message_row(i + 1) for i in range(41)]):
            self.data["messages"] = rows
            self.repo.memory_snapshot.return_value = self.data
            with self.assertRaises(MemoryDataError):
                self.memory.load(self.credentials)

    def test_database_outage_does_not_return_empty_memory(self):
        self.repo.memory_snapshot.side_effect = DatabaseError("connection failed")
        with self.assertRaises(DatabaseError):
            self.memory.load(self.credentials)

    def test_clear_and_delete_are_scoped_to_credentials(self):
        self.repo.clear_history.return_value = {"deleted_messages": 3}
        self.assertEqual(self.memory.clear_history(self.credentials), 3)
        self.repo.clear_history.assert_called_once_with(self.credentials)
        self.memory.delete_session(self.credentials)
        self.repo.delete_session.assert_called_once_with(self.credentials)

    def test_typed_state_validation_and_immutability(self):
        for changes in ({"booking_stage": "phone"}, {"facts": ["Тест"]},
                        {"facts": ("Тест",) * 21}, {"client_name": "x" * 101},
                        {"history_consent_at": "2026-10-16T10:00:00"},
                        {"history_consent_at": None}, {"facts": ("я" * 500,) * 20}):
            with self.assertRaises(ValueError):
                replace(self.state, **changes)
        with self.assertRaises(FrozenInstanceError):
            self.state.client_name = "Новое имя"
        with self.assertRaises(ValueError):
            ConversationState.from_dict({"consent": True})
        with self.assertRaises(ValueError):
            Memory(self.repo, history_limit=0)
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


def run_checks(execute_sql) -> None:
    """execute_sql работает только с отдельной временной БД из check_schema_local.py."""
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

    settings = SupabaseSettings("https://local-test.invalid", "sb_secret_" + "x" * 32)
    transport = httpx.MockTransport(bridge)
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
        with SupabaseClient(settings, httpx.MockTransport(bridge)) as restarted_client:
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
        migration = (ROOT / "database/migrations/002_memory.sql").read_text(encoding="utf-8")
        run(psql, sql=migration)
        run(psql, sql=migration)  # Повторное применение не должно открывать права.
        sys.path.insert(0, str(ROOT))
        from tests.memory_postgres_checks import run_checks
        run_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
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
