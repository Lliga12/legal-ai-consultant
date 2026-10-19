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
