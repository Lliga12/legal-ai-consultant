"""Полный диалог с настоящим временным PostgreSQL и тестовым ответом LLM."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from config import JudgeSettings, SupabaseSettings
from core.assistant import Assistant
from core.judge import Judge
from core.memory import HistoryConsentRequired, Memory
from core.router import BookingStage
from database.repositories import Repositories
from database.supabase_client import ConflictError, SupabaseClient
from llm.base import BaseLLM, LLMResponse
from tests.memory_postgres_checks import make_transport


def run_checks(execute_sql):
    with SupabaseClient(SupabaseSettings("https://local-test.invalid", "sb_secret_" + "x" * 32),
                        make_transport(execute_sql)) as client:
        repo = Repositories(client)
        model = Mock(spec=BaseLLM)
        model.generate.return_value = LLMResponse(
            "Для взыскания алиментов уточните, есть ли уже решение суда.", "test", "test")
        assistant = Assistant(Memory(repo), model, Judge(JudgeSettings(mode="local")))
        a, b = repo.new_credentials(), repo.new_credentials()
        try:
            assistant.start(a, consent=False)
            raise AssertionError("Missing history consent accepted")
        except HistoryConsentRequired:
            pass
        assert execute_sql(f"select count(*) from public.chat_sessions where id='{a.session_id}';") == "0"
        sa = assistant.start(a, consent=True)
        sb = assistant.start(b, consent=True)
        general = assistant.send(a, "Как взыскать алименты?", expected_version=sa.version)
        assert "решение суда" in general.answer, general.answer
        assert general.snapshot.state.legal_topic
        sa = general.snapshot
        price = assistant.send(a, "Сколько стоит консультация?", expected_version=sa.version)
        assert "индивидуально" in price.answer
        assert model.generate.call_count == 1
        sa = price.snapshot
        for text, stage in (("Хочу записаться на консультацию", BookingStage.TOPIC),
                            ("Нужно взыскать алименты, решения суда пока нет", BookingStage.NAME),
                            ("Тест", BookingStage.PHONE),
                            ("+70000000000", BookingStage.CONSENT)):
            turn = assistant.send(a, text, expected_version=sa.version)
            sa = turn.snapshot
            assert sa.state.booking_stage == stage, (stage, sa.state.booking_stage, turn.answer)
            if stage == BookingStage.PHONE:
                assert "телефон" in turn.answer, turn.answer
        request_id = sa.state.lead_request_id
        # Текстовое «да» не даёт согласие на регистрацию.
        sa = assistant.send(a, "да", expected_version=sa.version).snapshot
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "0"
        try:
            assistant.confirm(a, expected_version=sa.version, consent=False)
            raise AssertionError("Missing booking consent accepted")
        except HistoryConsentRequired:
            pass
        # Восстановление состояния новым объектом сервиса.
        restarted = Assistant(Memory(repo), model, Judge(JudgeSettings(mode="local")))
        restored = restarted.memory.load(a)
        assert restored.state.lead_request_id == request_id
        assert restored.state.client_name == "Тест"
        assert restored.state.booking_phone == "+70000000000"
        registered = restarted.confirm(a, expected_version=restored.version, consent=True)
        assert "Заявка зарегистрирована." in registered.answer, registered.answer
        repeat = restarted.confirm(a, expected_version=registered.snapshot.version, consent=True)
        assert "зарегистрирована ранее" in repeat.answer, repeat.answer
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "1"
        assert execute_sql(f"select count(*) from public.notification_logs n join public.leads l on l.id=n.lead_id where l.session_id='{a.session_id}';") == "1"
        assert "Уведомление отправлено" not in registered.answer
        # Контакты и переписка записи не передаются в LLM при новом общем вопросе.
        turn = restarted.send(a, "Как взыскать алименты?", expected_version=repeat.snapshot.version)
        payload = repr(model.generate.call_args)
        assert "+70000000000" not in payload and "Тест" not in payload, payload
        assert "есть ли уже решение суда" in payload, payload
        model.generate.return_value = LLMResponse("Консультация юриста стоит 5000 рублей.", "test", "test")
        turn = restarted.send(a, "Как взыскать алименты?", expected_version=turn.snapshot.version)
        assert "5000" not in turn.answer and "предварительный" in turn.answer
        assert restarted.memory.load(b).messages == ()
        # Два одинаковых устаревших события не запускают два хода.
        def stale(_):
            try:
                return restarted.send(b, "Сколько стоит развод?", expected_version=sb.version)
            except ConflictError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(stale, range(2)))
        assert sum(r is not None for r in results) == 1
        deleted = restarted.clear(a, expected_version=turn.snapshot.version, leads=True)
        assert deleted.state.lead_request_id is None
        assert execute_sql(f"select count(*) from public.leads where session_id='{a.session_id}';") == "0"
        assert deleted.messages  # отдельное удаление заявок не стирает переписку
        cleared = restarted.clear(a, expected_version=deleted.version)
        assert cleared.messages == () and not cleared.state.history_allowed
        try:
            restarted.send(a, "Привет", expected_version=cleared.version)
            raise AssertionError("Sending after clear without renewed consent")
        except HistoryConsentRequired:
            pass
        restarted.clear(a, expected_version=cleared.version, leads=True)
        c = repo.new_credentials()
        sc = restarted.start(c, consent=True)
        mixed = restarted.send(c, "Сколько стоит консультация? Хочу записаться.", expected_version=sc.version)
        assert "индивидуально" in mixed.answer and mixed.snapshot.state.booking_stage == BookingStage.TOPIC
        print("PASS: assembled assistant: staged booking, explicit consent, one lead/outbox, restart, contact minimization, stale events, deletion")


def serve_ui(execute_sql):
    """Стенд интерфейса: временная SQL-БД, LLM — заглушка, Telegram не отправляется."""
    from ui.gradio_app import Runtime, UI_CSS, create_app
    with SupabaseClient(SupabaseSettings("https://local-test.invalid", "sb_secret_" + "x" * 32),
                        make_transport(execute_sql)) as client:
        model = Mock(spec=BaseLLM)
        model.generate.return_value = LLMResponse(
            "Для взыскания алиментов уточните, есть ли уже решение суда.", "test", "test")
        assistant = Assistant(Memory(Repositories(client)), model, Judge(JudgeSettings(mode="local")))
        runtime = Runtime(assistant=assistant, issue="ЛОКАЛЬНЫЙ ТЕСТ: временная PostgreSQL; LLM подменена; Telegram не отправляется.")
        create_app(runtime).launch(server_name="127.0.0.1", server_port=7861, share=False,
                                   show_error=False, footer_links=[], css=UI_CSS)
