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
