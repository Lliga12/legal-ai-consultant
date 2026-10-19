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
        from config import ConfigError
        from ui.gradio_app import launch
        try:
            launch()
        except ConfigError as exc:
            raise SystemExit(str(exc)) from None
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
