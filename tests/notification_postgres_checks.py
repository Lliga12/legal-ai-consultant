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
