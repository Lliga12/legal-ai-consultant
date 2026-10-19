# Полный код этапа 8

Снимок исходных файлов для просмотра. Рабочие версии находятся по указанным путям. Действующих ключей и секретов сессий нет; токены в тестах синтетические.

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
```

## tools/telegram_tools.py

```python
"""Telegram Bot API. Сырые HTTP-ошибки, токены и описание дела не выводятся."""

from datetime import datetime, timedelta, timezone
import logging
import re

import httpx

from config import TelegramSettings
from core.router import normalize, topic_from
from database.repositories import text_value, uuid_text


class TelegramError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False,
                 uncertain: bool = False, retry_after: int | None = None):
        super().__init__("Telegram: " + code)
        self.code, self.retryable, self.uncertain = code, retryable, uncertain
        self.retry_after = retry_after


class _RedactBotToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = re.sub(r"bot[0-9]+:[A-Za-z0-9_-]+", "bot[REDACTED]", record.getMessage())
        record.args = ()
        return True


def format_notification(lead: dict, *, include_contact: bool = False) -> str:
    """Только поля сохранённой заявки; описание и свободная тема не пересылаются."""
    lead_id = uuid_text(lead["id"])
    name = text_value(lead["name"], "Имя", 100).split()[0][:40]
    if not name or any(not (c.isalpha() or c in "-'.") for c in name):
        name = "Клиент"
    phone = lead["phone"]
    if not isinstance(phone, str) or not re.fullmatch(r"\+7[0-9]{10}", phone):
        raise ValueError("Некорректный контакт заявки.")
    if not include_contact:
        phone = "+7 *** *** " + phone[-4:-2] + " " + phone[-2:]
    topic = topic_from(normalize(text_value(lead["legal_topic"], "Тема", 200)))
    topic = topic or "Общий юридический вопрос"
    stamp = text_value(lead["created_at"], "Дата", 50)
    date = None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            date = datetime.strptime(stamp, fmt)
            break
        except ValueError:
            continue
    if date is None:
        raise ValueError("Некорректная дата заявки.")
    local = date.astimezone(timezone(timedelta(hours=3)))
    return (f"Новая заявка № {lead_id}\nИмя: {name}\nКонтакт: {phone}\n"
            f"Тема: {topic}\nЗарегистрирована: {local:%d.%m.%Y %H:%M} (UTC+03:00)")


class TelegramClient:
    def __init__(self, settings: TelegramSettings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._http = httpx.Client(timeout=settings.timeout, transport=transport, follow_redirects=False)
        logger = logging.getLogger("httpx")
        if not any(isinstance(f, _RedactBotToken) for f in logger.filters):
            logger.addFilter(_RedactBotToken())

    def _call(self, method: str, payload: dict) -> dict:
        if method not in {"getMe", "getChat", "sendMessage"}:
            raise ValueError("Недопустимый метод Telegram.")
        error = None
        try:
            response = self._http.post(
                "https://api.telegram.org/bot" + self.settings.token + "/" + method, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            error = TelegramError("connection_failed", retryable=True)
        except httpx.RequestError:
            error = TelegramError("delivery_unknown", uncertain=True)
        if error is not None:
            raise error
        try:
            data = response.json()
        except ValueError:
            raise TelegramError("response_unknown", uncertain=True) from None
        if not isinstance(data, dict):
            raise TelegramError("response_unknown", uncertain=True)
        if data.get("ok") is False:
            code = data.get("error_code")
            if type(code) is not int:
                raise TelegramError("response_unknown", uncertain=True)
            if code == 429:
                params = data.get("parameters")
                retry = params.get("retry_after") if isinstance(params, dict) else None
                if retry is not None and (type(retry) is not int or not 1 <= retry <= 604800):
                    raise TelegramError("invalid_retry_after")
                raise TelegramError("api_429", retryable=True, retry_after=retry)
            raise TelegramError("api_" + str(code) if code in {400,401,403,404,500,502,503,504}
                                else "api_rejected", retryable=500 <= code <= 599)
        if (not 200 <= response.status_code < 300 or data.get("ok") is not True
                or not isinstance(data.get("result"), dict)):
            raise TelegramError("response_unknown", uncertain=True)
        return data["result"]

    def check(self) -> None:
        bot = self._call("getMe", {})
        chat = self._call("getChat", {"chat_id": self.settings.chat_id})
        if bot.get("is_bot") is not True or str(chat.get("id")) != self.settings.chat_id:
            raise TelegramError("unexpected_recipient")

    def send_text(self, text: str) -> int:
        if not isinstance(text,str) or not 1 <= len(text) <= 4096:
            raise ValueError("Текст Telegram должен содержать от 1 до 4096 символов.")
        result = self._call("sendMessage", {"chat_id":self.settings.chat_id,"text":text,
            "protect_content":True,"link_preview_options":{"is_disabled":True}})
        chat = result.get("chat")
        if (type(result.get("message_id")) is not int or result["message_id"] <= 0
                or not isinstance(chat,dict) or str(chat.get("id")) != self.settings.chat_id):
            raise TelegramError("response_unknown", uncertain=True)
        return result["message_id"]

    def send_telegram_notification(self, lead: dict) -> int:
        return self.send_text(format_notification(lead, include_contact=self.settings.include_contact))

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def send_telegram_notification(lead: dict, *, client: TelegramClient) -> int:
    """Явная зависимость от настроенного клиента; без скрытых глобальных ключей."""
    return client.send_telegram_notification(lead)
```

## tools/telegram_setup.py

```python
"""Получить ID чатов своего бота без токена в командной строке и без отправки сообщений."""

import logging
import os
import re

import httpx
from dotenv import load_dotenv

from config import ROOT
from tools.telegram_tools import _RedactBotToken


def discover_chats(token: str, transport: httpx.BaseTransport | None = None) -> dict[int,str]:
    if not isinstance(token,str) or not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}",token):
        raise ValueError("Заполните TELEGRAM_BOT_TOKEN в .env.")
    logging.getLogger("httpx").addFilter(_RedactBotToken())
    try:
        with httpx.Client(timeout=10,transport=transport,follow_redirects=False) as client:
            response = client.post("https://api.telegram.org/bot"+token+"/getUpdates",
                                   json={"timeout":0,"limit":100})
        data = response.json()
    except (httpx.RequestError,ValueError):
        raise ValueError("Не удалось получить чаты Telegram. Проверьте сеть и токен.") from None
    if (not 200<=response.status_code<300 or not isinstance(data,dict) or data.get("ok") is not True
            or not isinstance(data.get("result"),list)):
        raise ValueError("Telegram не вернул список чатов. Проверьте токен и отсутствие активного webhook.")
    chats = {}
    for update in data["result"]:
        if not isinstance(update,dict):
            continue
        message = update.get("message")
        chat = message.get("chat") if isinstance(message,dict) else None
        if (isinstance(chat,dict) and type(chat.get("id")) is int
                and chat.get("type") in {"private","group","supergroup"}):
            chats[chat["id"]] = chat["type"]
    return chats


def main() -> None:
    load_dotenv(ROOT/".env",override=False,interpolate=False,encoding="utf-8-sig")
    try:
        chats = discover_chats(os.getenv("TELEGRAM_BOT_TOKEN","").strip())
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if not chats:
        print("Чаты не найдены. Напишите /start своему боту и повторите команду.")
    for chat_id,kind in sorted(chats.items()):
        print(f"TELEGRAM_CHAT_ID={chat_id} ({kind})")


if __name__=="__main__":
    main()
```

## tools/notification_worker.py

```python
"""Обработчик постоянной очереди. Автоматические повторы — только при явном отказе API."""

from dataclasses import dataclass

from database.repositories import Repositories
from database.supabase_client import DatabaseError
from tools.telegram_tools import TelegramClient, TelegramError, send_telegram_notification


@dataclass(frozen=True)
class DeliveryResult:
    status: str


class NotificationWorker:
    def __init__(self, repositories: Repositories, telegram: TelegramClient):
        self.repositories, self.telegram = repositories, telegram

    def run_once(self) -> DeliveryResult:
        settings = self.telegram.settings
        job = self.repositories.claim_notification(settings.lease_seconds, settings.max_attempts)
        if job is None:
            return DeliveryResult("idle")
        lead = self.repositories.notification_current(job["id"],job["claim_token"])
        if lead is None:
            return DeliveryResult("cancelled_or_expired")
        message_id = None
        error_code = None
        retry_delay = None
        cooldown_delay = None
        try:
            message_id = send_telegram_notification(lead,client=self.telegram)
        except TelegramError as exc:
            error_code = exc.code
            if exc.code == "api_429":
                cooldown_delay = max(exc.retry_after or 0,60)
            if exc.retryable and not exc.uncertain and job["attempts"] < settings.max_attempts:
                retry_delay = max(exc.retry_after or 0,min(60 * 2**(job["attempts"]-1),3600))
        except (ValueError,KeyError,TypeError):
            error_code = "invalid_payload"
        try:
            recorded = self.repositories.finish_notification(job["id"],job["claim_token"],
                sent=message_id is not None,message_id=message_id,error_code=error_code,retry_delay=retry_delay,
                cooldown_delay=cooldown_delay)
        except DatabaseError:
            # Не повторяем отправку: Telegram мог уже принять сообщение.
            return DeliveryResult("unconfirmed")
        if not recorded:
            return DeliveryResult("unconfirmed")
        if message_id is not None:
            return DeliveryResult("sent")
        return DeliveryResult("retry_scheduled" if retry_delay is not None else "needs_attention")

    def run_batch(self, limit: int = 10) -> tuple[DeliveryResult,...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Размер пакета: от 1 до 100 задач.")
        results = []
        for _ in range(limit):
            result = self.run_once()
            if result.status == "idle":
                break
            results.append(result)
            if result.status == "unconfirmed":
                break
        return tuple(results)
```

## database/migrations/004_telegram.sql

```sql
-- Этап 8. После миграций 002 и 003. Повторное применение допустимо.
begin;
alter table public.notification_logs add column if not exists claim_token uuid;
alter table public.notification_logs add column if not exists telegram_message_id bigint;
alter table public.notification_logs alter column next_attempt_at drop not null;

-- Общий cooldown для единственного настроенного бота/чата, переживает перезапуск.
create table if not exists public.telegram_delivery_state (
    id text primary key check (id='lawyer'),
    retry_after timestamptz
);
insert into public.telegram_delivery_state(id) values ('lawyer') on conflict do nothing;
alter table public.telegram_delivery_state enable row level security;
revoke all on public.telegram_delivery_state from public,anon,authenticated;
grant select,insert,update,delete on public.telegram_delivery_state to service_role;

create or replace function public.legal_ai_claim_notification(
    p_lease_seconds integer default 120, p_max_attempts integer default 5)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare n public.notification_logs;
begin
    if p_lease_seconds not between 120 and 600 or p_lease_seconds is null
        or p_max_attempts not between 1 and 10 or p_max_attempts is null then
        raise exception 'invalid notification limits' using errcode='22023';
    end if;
    -- Истёкшая аренда означает неизвестный результат, а не разрешение отправить снова.
    update public.notification_logs set status='failed',error_message='lease_expired_unknown',
        next_attempt_at=null,locked_until=null,claim_token=null
        where status='sending' and (locked_until <= clock_timestamp() or locked_until is null);
    if exists(select 1 from public.telegram_delivery_state
        where id='lawyer' and retry_after>clock_timestamp()) then return null; end if;
    select * into n from public.notification_logs
        where status in ('pending','failed') and next_attempt_at <= clock_timestamp()
            and attempts < p_max_attempts
        order by next_attempt_at,created_at,id for update skip locked limit 1;
    if not found then return null; end if;
    update public.notification_logs set status='sending',attempts=attempts+1,
        claim_token=gen_random_uuid(),locked_until=clock_timestamp()+make_interval(secs=>p_lease_seconds),
        error_message=null where id=n.id returning * into n;
    return jsonb_build_object('id',n.id,'claim_token',n.claim_token,'attempts',n.attempts);
end;
$$;

create or replace function public.legal_ai_notification_current(p_notification_id uuid,p_claim_token uuid)
returns jsonb language sql security invoker set search_path = '' as $$
    -- Без описания дела, истории, токена сессии и других лишних данных.
    select jsonb_build_object('id',l.id,'name',l.name,'phone',l.phone,
        'legal_topic',l.legal_topic,'created_at',l.created_at)
    from public.notification_logs n join public.leads l on l.id=n.lead_id
    where n.id=p_notification_id and n.claim_token=p_claim_token
        and n.status='sending' and n.locked_until > clock_timestamp();
$$;

create or replace function public.legal_ai_finish_notification(
    p_notification_id uuid,p_claim_token uuid,p_sent boolean,
    p_message_id bigint default null,p_error_code text default null,p_retry_delay integer default null,
    p_cooldown_delay integer default null)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare changed integer;
begin
    if p_sent is null or (p_sent and (p_message_id is null or p_message_id<=0
        or p_error_code is not null or p_retry_delay is not null or p_cooldown_delay is not null))
        or (not p_sent and (p_message_id is not null or p_error_code is null
            or p_error_code not in ('connection_failed','delivery_unknown','response_unknown','api_400',
                'api_401','api_403','api_404','api_429','api_500','api_502','api_503','api_504',
                'api_rejected','invalid_retry_after','unexpected_recipient','invalid_payload')))
        or (p_retry_delay is not null and p_retry_delay not between 1 and 604800)
        or (p_cooldown_delay is not null and (p_cooldown_delay not between 1 and 604800
            or p_error_code is distinct from 'api_429')) then
        raise exception 'invalid delivery result' using errcode='22023';
    end if;
    update public.notification_logs set status=case when p_sent then 'sent' else 'failed' end,
        telegram_message_id=p_message_id,sent_at=case when p_sent then now() else null end,
        error_message=p_error_code,next_attempt_at=case when not p_sent and p_retry_delay is not null
            then clock_timestamp()+make_interval(secs=>p_retry_delay) else null end,
        locked_until=null,claim_token=null
        where id=p_notification_id and claim_token=p_claim_token and status='sending';
    get diagnostics changed=row_count;
    if changed=1 and p_cooldown_delay is not null then
        update public.telegram_delivery_state set retry_after=greatest(retry_after,
            clock_timestamp()+make_interval(secs=>p_cooldown_delay)) where id='lawyer';
    end if;
    return jsonb_build_object('recorded',changed=1);
end;
$$;

create or replace function public.legal_ai_notifications_health()
returns jsonb language sql security invoker set search_path = '' as $$
    select jsonb_build_object('notifications_version',1,'rpc_ready',
        (select count(*)=4 and not bool_or(p.prosecdef)
            and not bool_or(pg_catalog.has_function_privilege('anon',p.oid,'EXECUTE'))
            and not bool_or(pg_catalog.has_function_privilege('authenticated',p.oid,'EXECUTE'))
         from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid=p.pronamespace
         where n.nspname='public' and p.proname in ('legal_ai_claim_notification',
            'legal_ai_notification_current','legal_ai_finish_notification','legal_ai_notifications_health'))
        and exists(select 1 from pg_catalog.pg_attribute where attrelid='public.notification_logs'::regclass
            and attname='claim_token' and not attisdropped)
        and exists(select 1 from pg_catalog.pg_attribute where attrelid='public.notification_logs'::regclass
            and attname='telegram_message_id' and not attisdropped)
        and (select relrowsecurity from pg_catalog.pg_class where oid='public.telegram_delivery_state'::regclass)
        and not pg_catalog.has_table_privilege('anon','public.telegram_delivery_state','SELECT,INSERT,UPDATE,DELETE')
        and not pg_catalog.has_table_privilege('authenticated','public.telegram_delivery_state','SELECT,INSERT,UPDATE,DELETE'));
$$;

revoke all on function public.legal_ai_claim_notification(integer,integer),
    public.legal_ai_notification_current(uuid,uuid),
    public.legal_ai_finish_notification(uuid,uuid,boolean,bigint,text,integer,integer),
    public.legal_ai_notifications_health() from public,anon,authenticated;
grant execute on function public.legal_ai_claim_notification(integer,integer),
    public.legal_ai_notification_current(uuid,uuid),
    public.legal_ai_finish_notification(uuid,uuid,boolean,bigint,text,integer,integer),
    public.legal_ai_notifications_health() to service_role;
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
                   "lead_request", "lead_consent", "submit_lead", "tools_health",
                   "claim_notification", "notification_current", "finish_notification", "notifications_health"}
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
"""Проверки и обработчик уведомлений (этапы 1–8)."""

import argparse
from pathlib import Path
import sys


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Требуется Python 3.10 или новее.")
    parser = argparse.ArgumentParser(description="AI-консультант юриста: этап 8")
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
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    required = ("knowledge", "core", "llm", "tools", "database", "ui", "tests")
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise SystemExit("Отсутствуют каталоги: " + ", ".join(missing))
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
        print("AI-консультант юриста — структура проекта готова.")
        print("Этап 8. Проверки: --check-router, --check-llm, --check-db, --check-memory, --check-tools, --check-notifications, --check-telegram.")
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
# Telegram: бот и числовой ID адресата (только сервер).
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_TIMEOUT_SECONDS=10
# По умолчанию телефон маскируется; true включает полный контакт в сообщении.
TELEGRAM_INCLUDE_CONTACT=false
NOTIFICATION_MAX_ATTEMPTS=5
NOTIFICATION_LEASE_SECONDS=120
```

## tests/test_telegram.py

```python
"""Telegram/worker: никакие внешние сообщения в этих тестах не отправляются."""

from dataclasses import replace
import json
import os
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from config import ConfigError, TelegramSettings, load_telegram_settings
from database.repositories import Repositories
from database.supabase_client import DatabaseError
from tools.notification_worker import NotificationWorker
from tools.telegram_tools import TelegramClient, TelegramError, format_notification, send_telegram_notification
from tools.telegram_setup import discover_chats


TOKEN = "123456789:" + "x"*35


class TelegramTests(TestCase):
    def setUp(self):
        self.settings = TelegramSettings(TOKEN,"123456")
        self.lead = {"id":str(uuid4()),"name":"Тестовое Имя","phone":"+70000001234",
            "legal_topic":"Алименты","description":"Секретное описание", "created_at":"2026-10-16T10:00:00.12345Z"}

    def client(self, handler):
        return TelegramClient(self.settings,httpx.MockTransport(handler))

    def success(self, request):
        return httpx.Response(200,json={"ok":True,"result":{"message_id":10,"chat":{"id":123456}}})

    def test_minimal_notification_and_time(self):
        text = format_notification(self.lead)
        self.assertIn("Тестовое",text)
        self.assertNotIn("Тестовое Имя",text)
        self.assertIn("Контакт: +7 *** *** 12 34",text)
        self.assertIn("16.10.2026 13:00",text)
        self.assertIn("Взыскание алиментов",text)
        self.assertNotIn("Секретное описание",text)
        self.assertNotIn(self.lead["phone"],text)
        self.assertIn(self.lead["phone"],format_notification(self.lead,include_contact=True))

    def test_freeform_topic_and_invalid_name_not_forwarded(self):
        self.lead["legal_topic"] = "Позвоните +70000001234, private@example.com"
        self.lead["name"] = "private@example.com"
        text = format_notification(self.lead)
        self.assertNotIn("private@example.com",text)
        self.assertIn("Имя: Клиент",text)
        self.assertIn("Общий юридический вопрос",text)

    def test_send_payload_and_protected_content(self):
        def handler(request):
            self.assertTrue(str(request.url).endswith("/sendMessage"))
            body = json.loads(request.content)
            self.assertEqual(body["chat_id"],"123456")
            self.assertTrue(body["protect_content"])
            self.assertNotIn("parse_mode",body)
            self.assertNotIn("allow_paid_broadcast",body)
            return self.success(request)
        with self.client(handler) as client:
            self.assertEqual(send_telegram_notification(self.lead,client=client),10)

    def test_http_logs_redact_bot_token(self):
        with self.client(self.success) as client, self.assertLogs("httpx",level="INFO") as logs:
            client.send_telegram_notification(self.lead)
        self.assertNotIn(TOKEN," ".join(logs.output))
        self.assertIn("REDACTED"," ".join(logs.output))
        self.assertNotIn(TOKEN,repr(self.settings))

    def test_bot_and_chat_check_without_send(self):
        methods = []
        def handler(request):
            methods.append(request.url.path.rsplit("/",1)[-1])
            result = {"is_bot":True,"id":111} if methods[-1] == "getMe" else {"id":123456}
            return httpx.Response(200,json={"ok":True,"result":result})
        with self.client(handler) as client:
            client.check()
        self.assertEqual(methods,["getMe","getChat"])

    def test_api_errors_are_safe(self):
        for code in (400,401,403,429,500):
            def handler(request):
                return httpx.Response(code,json={"ok":False,"error_code":code,"description":TOKEN,
                    "parameters":{"retry_after":90}})
            with self.client(handler) as client:
                with self.assertRaises(TelegramError) as error:
                    client.send_telegram_notification(self.lead)
                self.assertNotIn(TOKEN,str(error.exception))
                self.assertFalse(error.exception.uncertain)
                self.assertEqual(error.exception.retryable,code in {429,500})
                if code == 429:
                    self.assertEqual(error.exception.retry_after,90)

    def test_timeout_and_response_ambiguity(self):
        def timeout(request):
            raise httpx.ReadTimeout(TOKEN,request=request)
        with self.client(timeout) as client:
            with self.assertRaises(TelegramError) as error:
                client.send_telegram_notification(self.lead)
            self.assertTrue(error.exception.uncertain)
            self.assertIsNone(error.exception.__context__)
        responses = [httpx.Response(200,text="bad"),httpx.Response(200,json=[]),
            httpx.Response(200,json={"ok":True,"result":{}}),
            httpx.Response(200,json={"ok":True,"result":{"message_id":1,"chat":{"id":999}}})]
        for response in responses:
            with self.client(lambda r: response) as client:
                with self.assertRaises(TelegramError) as error:
                    client.send_telegram_notification(self.lead)
                self.assertTrue(error.exception.uncertain)

    def test_connect_failure_is_retryable(self):
        def handler(request):
            raise httpx.ConnectError(TOKEN,request=request)
        with self.client(handler) as client:
            with self.assertRaises(TelegramError) as error:
                client.send_telegram_notification(self.lead)
            self.assertTrue(error.exception.retryable)
            self.assertFalse(error.exception.uncertain)

    def test_oversized_or_invalid_retry_after_halts(self):
        for value in (True,-1,604801,"90"):
            with self.client(lambda r: httpx.Response(429,json={"ok":False,"error_code":429,
                    "parameters":{"retry_after":value}})) as client:
                with self.assertRaises(TelegramError) as error:
                    client.send_telegram_notification(self.lead)
                self.assertFalse(error.exception.retryable)

    def test_telegram_config_validation(self):
        for changes in ({"token":"bad"},{"chat_id":"@username"},{"chat_id":"0"},
                        {"timeout":0},{"max_attempts":11},{"lease_seconds":30}):
            with self.assertRaises(ConfigError):
                replace(self.settings,**changes)
        env = {"TELEGRAM_BOT_TOKEN":TOKEN,"TELEGRAM_CHAT_ID":"123456"}
        with patch.dict(os.environ,env,clear=True),patch("config.load_dotenv"):
            self.assertFalse(load_telegram_settings().include_contact)

    def test_chat_discovery_only_returns_ids_and_types(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertNotIn("offset",payload)
            self.assertTrue(str(request.url).endswith("/getUpdates"))
            return httpx.Response(200,json={"ok":True,"result":[
                {"message":{"text":"private content","chat":{"id":123456,"type":"private","first_name":"Private"}}},
                {"message":{"chat":{"id":-123456,"type":"group"}}}]})
        result = discover_chats(TOKEN,httpx.MockTransport(handler))
        self.assertEqual(result,{123456:"private",-123456:"group"})
        self.assertNotIn("private content",str(result))

    def test_chat_discovery_errors_do_not_expose_token(self):
        def handler(request):
            raise httpx.ConnectError(TOKEN,request=request)
        with self.assertRaises(ValueError) as error:
            discover_chats(TOKEN,httpx.MockTransport(handler))
        self.assertNotIn(TOKEN,str(error.exception))


class WorkerTests(TestCase):
    def setUp(self):
        self.repo = Mock(spec=Repositories)
        self.telegram = Mock(spec=TelegramClient)
        self.telegram.settings = TelegramSettings(TOKEN,"123456")
        self.telegram.send_telegram_notification.return_value = 10
        self.job = {"id":str(uuid4()),"claim_token":str(uuid4()),"attempts":1}
        self.repo.claim_notification.return_value = self.job
        self.repo.notification_current.return_value = {"id":str(uuid4())}
        self.repo.finish_notification.return_value = True
        self.worker = NotificationWorker(self.repo,self.telegram)

    def test_delivery_recorded_after_send(self):
        self.assertEqual(self.worker.run_once().status,"sent")
        self.repo.finish_notification.assert_called_once_with(self.job["id"],self.job["claim_token"],
            sent=True,message_id=10,error_code=None,retry_delay=None,cooldown_delay=None)

    def test_empty_or_deleted_jobs_do_not_send(self):
        self.repo.claim_notification.return_value = None
        self.assertEqual(self.worker.run_once().status,"idle")
        self.repo.claim_notification.return_value = self.job
        self.repo.notification_current.return_value = None
        self.assertEqual(self.worker.run_once().status,"cancelled_or_expired")
        self.telegram.send_telegram_notification.assert_not_called()

    def test_known_failure_schedules_retry(self):
        self.telegram.send_telegram_notification.side_effect = TelegramError("api_429",retryable=True,retry_after=90)
        self.assertEqual(self.worker.run_once().status,"retry_scheduled")
        self.assertEqual(self.repo.finish_notification.call_args.kwargs["retry_delay"],90)
        self.assertEqual(self.repo.finish_notification.call_args.kwargs["cooldown_delay"],90)

    def test_uncertain_or_permanent_failure_halts(self):
        for error in (TelegramError("delivery_unknown",uncertain=True),TelegramError("api_403")):
            self.telegram.send_telegram_notification.side_effect = error
            self.assertEqual(self.worker.run_once().status,"needs_attention")
            self.assertIsNone(self.repo.finish_notification.call_args.kwargs["retry_delay"])

    def test_last_attempt_halts(self):
        self.job["attempts"] = 5
        self.telegram.send_telegram_notification.side_effect = TelegramError("api_500",retryable=True)
        self.assertEqual(self.worker.run_once().status,"needs_attention")
        self.assertIsNone(self.repo.finish_notification.call_args.kwargs["retry_delay"])

    def test_db_error_after_send_never_repeats_send(self):
        self.repo.finish_notification.side_effect = DatabaseError("database offline")
        self.assertEqual(self.worker.run_once().status,"unconfirmed")
        self.assertEqual(self.telegram.send_telegram_notification.call_count,1)

    def test_lost_fencing_token_does_not_claim_success(self):
        self.repo.finish_notification.return_value = False
        self.assertEqual(self.worker.run_once().status,"unconfirmed")

    def test_batch_stops_on_unconfirmed_delivery(self):
        self.repo.finish_notification.side_effect = DatabaseError("offline")
        self.assertEqual(len(self.worker.run_batch()),1)
        self.assertEqual(self.telegram.send_telegram_notification.call_count,1)

    def test_repository_rejects_sensitive_error_text(self):
        repo = Repositories(Mock())
        with self.assertRaises(ValueError):
            repo.finish_notification(self.job["id"],self.job["claim_token"],sent=False,error_code=TOKEN)
```

## tests/notification_postgres_checks.py

```python
"""Настоящая очередь PostgreSQL + Telegram MockTransport. Внешних сообщений нет."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from uuid import uuid4

import httpx

from config import SupabaseSettings, TelegramSettings
from core.memory import Memory
from database.repositories import Repositories
from database.supabase_client import DatabaseError, SupabaseClient
from tests.memory_postgres_checks import make_transport
from tools.lead_tools import LeadTools
from tools.notification_worker import NotificationWorker
from tools.telegram_tools import TelegramClient


def run_checks(execute_sql) -> None:
    db_settings = SupabaseSettings("https://local-test.invalid","sb_secret_"+"x"*32)
    tg_settings = TelegramSettings("123456789:"+"x"*35,"123456")
    with SupabaseClient(db_settings,make_transport(execute_sql)) as client:
        repo = Repositories(client)
        repo.health()
        repo.notifications_health()
        sessions = []

        def new_lead(*, via_tool=False):
            credentials = repo.new_credentials()
            sessions.append(credentials)
            data = dict(name="Тестовое имя",phone="+70000001234",legal_topic="Алименты",
                        description="Синтетическое описание, не пересылать")
            if via_tool:
                snap = Memory(repo).initialize(credentials,history_consent=True,notice_version="synthetic-notice")
                tools = LeadTools(repo,credentials,"synthetic-lead-consent")
                draft = tools.prepare(**data,expected_version=snap.version)
                tools.record_consent(draft.request_id,consent=True,expected_version=draft.session_version)
                result = tools.create_lead(**data,session_id=credentials.session_id,consent=True,
                                           request_id=draft.request_id)
                assert result.success
                lead_id = result.lead_id
            else:
                repo.create_session(credentials)
                lead_id = repo.create_lead(credentials,idempotency_key=str(uuid4()),**data,
                                            consent=True,consent_version="synthetic-lead-consent")["id"]
            return credentials,lead_id

        def success(request):
            return httpx.Response(200,json={"ok":True,"result":{"message_id":101,"chat":{"id":123456}}})

        credentials,lead_id = new_lead(via_tool=True)
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = list(pool.map(lambda _:repo.claim_notification(),range(6)))
        claimed = [job for job in jobs if job is not None]
        assert len(claimed)==1
        job = claimed[0]
        payload = repo.notification_current(job["id"],job["claim_token"])
        assert set(payload)=={"id","name","phone","legal_topic","created_at"}
        assert payload["id"]==lead_id
        with TelegramClient(tg_settings,httpx.MockTransport(success)) as telegram:
            message_id = telegram.send_telegram_notification(payload)
        assert repo.finish_notification(job["id"],job["claim_token"],sent=True,message_id=message_id)
        assert not repo.finish_notification(job["id"],job["claim_token"],sent=True,message_id=message_id)
        assert repo.claim_notification() is None

        credentials,lead_id = new_lead(via_tool=True)
        with TelegramClient(tg_settings,httpx.MockTransport(success)) as telegram:
            assert NotificationWorker(repo,telegram).run_once().status=="sent"
        assert execute_sql(f"select status,telegram_message_id from public.notification_logs where lead_id='{lead_id}';")=="sent|101"

        credentials,lead_id = new_lead()
        calls = []
        def limited(request):
            calls.append(request)
            if len(calls)==1:
                return httpx.Response(429,json={"ok":False,"error_code":429,"parameters":{"retry_after":90}})
            return success(request)
        with TelegramClient(tg_settings,httpx.MockTransport(limited)) as telegram:
            assert NotificationWorker(repo,telegram).run_once().status=="retry_scheduled"
            assert execute_sql(f"select status,next_attempt_at>now() from public.notification_logs where lead_id='{lead_id}';")=="failed|t"
            _,another_lead_id = new_lead()
            assert repo.claim_notification() is None  # Cooldown действует и на другую задачу.
            execute_sql(f"update public.notification_logs set next_attempt_at=now()-interval '1 second' where lead_id='{lead_id}';")
            execute_sql("update public.telegram_delivery_state set retry_after=now()-interval '1 second' where id='lawyer';")
            with SupabaseClient(db_settings,make_transport(execute_sql)) as restarted:
                assert NotificationWorker(Repositories(restarted),telegram).run_once().status=="sent"
            assert NotificationWorker(repo,telegram).run_once().status=="sent"
        assert len(calls)==3

        credentials,lead_id = new_lead()
        uncertain_calls = []
        def uncertain(request):
            uncertain_calls.append(request)
            raise httpx.ReadTimeout("synthetic",request=request)
        with TelegramClient(tg_settings,httpx.MockTransport(uncertain)) as telegram:
            worker = NotificationWorker(repo,telegram)
            assert worker.run_once().status=="needs_attention"
            assert worker.run_once().status=="idle"
        assert len(uncertain_calls)==1
        assert execute_sql(f"select status,error_message,next_attempt_at is null from public.notification_logs where lead_id='{lead_id}';")=="failed|delivery_unknown|t"
        assert execute_sql(f"select count(*) from public.leads where id='{lead_id}';")=="1"

        credentials,lead_id = new_lead()
        job = repo.claim_notification()
        execute_sql(f"update public.notification_logs set locked_until=now()-interval '1 second' where id='{job['id']}';")
        assert repo.claim_notification() is None
        assert repo.notification_current(job["id"],job["claim_token"]) is None
        assert not repo.finish_notification(job["id"],job["claim_token"],sent=True,message_id=101)
        assert execute_sql(f"select error_message,next_attempt_at is null from public.notification_logs where lead_id='{lead_id}';")=="lease_expired_unknown|t"

        credentials,lead_id = new_lead()
        execute_sql(f"update public.notification_logs set attempts=4 where lead_id='{lead_id}';")
        with TelegramClient(tg_settings,httpx.MockTransport(lambda r:httpx.Response(500,json={"ok":False,"error_code":500}))) as telegram:
            worker = NotificationWorker(repo,telegram)
            assert worker.run_once().status=="needs_attention"
            assert worker.run_once().status=="idle"
        assert execute_sql(f"select attempts,next_attempt_at is null from public.notification_logs where lead_id='{lead_id}';")=="5|t"

        credentials,lead_id = new_lead()
        proxy = Mock(wraps=repo)
        proxy.finish_notification.side_effect = DatabaseError("synthetic database outage")
        sent_calls = []
        def counted(request):
            sent_calls.append(request)
            return success(request)
        with TelegramClient(tg_settings,httpx.MockTransport(counted)) as telegram:
            assert NotificationWorker(proxy,telegram).run_once().status=="unconfirmed"
            execute_sql(f"update public.notification_logs set locked_until=now()-interval '1 second' where lead_id='{lead_id}';")
            assert NotificationWorker(repo,telegram).run_once().status=="idle"
        assert len(sent_calls)==1

        credentials,lead_id = new_lead()
        job = repo.claim_notification()
        repo.delete_session(credentials)
        sessions.remove(credentials)
        assert repo.notification_current(job["id"],job["claim_token"]) is None
        assert not repo.finish_notification(job["id"],job["claim_token"],sent=True,message_id=101)
        for credentials in sessions:
            repo.delete_session(credentials)
    print("PASS: notification claims are exclusive; Tool->outbox->worker->Telegram stub->sent; retry_after survives restart")
    print("PASS: unknown delivery and expired leases halt; attempt limit, stale token, deletion and DB failure after send")
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
            raw = execute_sql(sql)
            data = json.loads(raw) if raw else None  # SQL NULL соответствует JSON null PostgREST.
        except RuntimeError as exc:
            match = re.search(r"\b(P0002|40001|23505|22023)\b", str(exc))
            return httpx.Response(409 if match else 500,
                                  json={"code": match.group(0) if match else "test_sql_failure"})
        # json=None в httpx означает отсутствие body; PostgREST для SQL NULL возвращает "null".
        return httpx.Response(200,content=json.dumps(data,ensure_ascii=False).encode("utf-8"),
                              headers={"content-type":"application/json"})

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
        migration = (ROOT / "database/migrations/004_telegram.sql").read_text(encoding="utf-8")
        run(psql, sql=migration)
        run(psql, sql=migration)
        from tests.notification_postgres_checks import run_checks as run_notification_checks
        run_notification_checks(lambda sql: run([*psql, "-v", "VERBOSITY=sqlstate"], sql=sql))
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
