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
