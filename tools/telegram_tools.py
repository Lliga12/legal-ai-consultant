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
