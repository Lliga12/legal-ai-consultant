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
