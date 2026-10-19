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
