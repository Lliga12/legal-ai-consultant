"""Границы сборки; постоянное хранение проверяет PostgreSQL-runner."""

from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from config import JudgeSettings
from core.assistant import Assistant
from core.judge import Judge
from core.memory import ConversationState, HistoryConsentRequired, Memory, MemorySnapshot
from core.router import BookingStage
from database.repositories import SessionCredentials
from database.supabase_client import ConflictError, DatabaseError
from llm.base import LLMError


def make_assistant():
    return Assistant(Mock(spec=Memory), Mock(), Judge(JudgeSettings(mode="local")))


@pytest.mark.parametrize("value", [None, "", " " * 3, "x" * 1501])
def test_invalid_input_before_database(value):
    assistant = make_assistant()
    with pytest.raises(ValueError):
        assistant.send(SessionCredentials(str(uuid4()), "a" * 43), value, expected_version=0)
    assistant.memory.load.assert_not_called()


@pytest.mark.parametrize("consent", [False, None, "да", 1])
def test_confirmation_requires_boolean_true(consent):
    assistant = make_assistant()
    with pytest.raises(HistoryConsentRequired):
        assistant.confirm(None, expected_version=0, consent=consent)
    assistant.memory.load.assert_not_called()


def test_old_memory_state_is_read_and_upgraded():
    old = ConversationState().to_dict()
    old["memory_schema"] = 1
    for key in ("booking_description", "booking_phone", "lead_request_id"):
        old.pop(key)
    loaded = ConversationState.from_dict(old)
    assert loaded == ConversationState()
    assert loaded.to_dict()["memory_schema"] == 2


def test_booking_state_private_fields_hidden_in_repr():
    state = replace(ConversationState(), client_name="СекретноеИмя", booking_phone="+70000000000",
                    booking_description="СекретноеОписание", lead_request_id=str(uuid4()))
    assert "Секретное" not in repr(state) and "+70000000000" not in repr(state)


def test_unknown_memory_v2_field_rejected():
    data = ConversationState().to_dict() | {"consent": True}
    with pytest.raises(ValueError):
        ConversationState.from_dict(data)


def initialized_assistant():
    assistant = make_assistant()
    credentials = SessionCredentials(str(uuid4()), "a" * 43)
    state = ConversationState(history_notice_version="test-history-v1",
                              history_consent_at="2026-10-16T10:00:00+00:00")
    snapshot = MemorySnapshot(credentials.session_id, 3, state, ())
    assistant.memory.load.return_value = snapshot
    return assistant, credentials, snapshot


def test_model_failure_shows_safe_reason_without_retry():
    assistant, credentials, snapshot = initialized_assistant()
    assistant.memory.append.return_value = snapshot
    assistant.memory.save_state.return_value = snapshot
    assistant.llm.generate.side_effect = LLMError(
        "LLM API вернул HTTP 429. Проверьте доступ и квоту.")
    turn = assistant.send(credentials, "Магазин отказался принять неисправный чайник.",
                          expected_version=snapshot.version)
    assert "HTTP 429" in turn.notice
    assert "Автоматический повтор не выполнялся" in turn.notice
    assistant.llm.generate.assert_called_once()
    assert assistant.memory.append.call_count == 2


@pytest.mark.parametrize("version", [2, True, None, "3"])
def test_stale_or_untyped_event_never_stores_or_calls_model(version):
    assistant, credentials, _ = initialized_assistant()
    with pytest.raises(ConflictError):
        assistant.send(credentials, "Как взыскать алименты?", expected_version=version)
    assistant.memory.append.assert_not_called()
    assistant.llm.generate.assert_not_called()


@pytest.mark.parametrize("version", [None, "obsolete-notice"])
def test_missing_or_obsolete_history_consent_stops_before_storage(version):
    assistant, credentials, snapshot = initialized_assistant()
    state = ConversationState(history_notice_version=version,
        history_consent_at="2026-10-16T10:00:00+00:00" if version else None)
    assistant.memory.load.return_value = replace(snapshot, state=state)
    with pytest.raises(HistoryConsentRequired):
        assistant.send(credentials, "Как взыскать алименты?", expected_version=3)
    assistant.memory.append.assert_not_called()
    assistant.llm.generate.assert_not_called()


def test_database_read_outage_never_calls_model_or_returns_fictional_history():
    assistant, credentials, _ = initialized_assistant()
    assistant.memory.load.side_effect = DatabaseError("Synthetic database outage")
    with pytest.raises(DatabaseError):
        assistant.send(credentials, "Как взыскать алименты?", expected_version=3)
    assistant.memory.append.assert_not_called()
    assistant.memory.save_state.assert_not_called()
    assistant.llm.generate.assert_not_called()


def test_failed_session_start_is_not_reported_as_ready():
    assistant, credentials, _ = initialized_assistant()
    assistant.memory.initialize.side_effect = DatabaseError("Synthetic database outage")
    with pytest.raises(DatabaseError):
        assistant.start(credentials, consent=True)
    assistant.llm.generate.assert_not_called()


@pytest.mark.parametrize("stage", [BookingStage.IDLE, BookingStage.CONSENT])
def test_confirmation_without_server_draft_cannot_call_tool(stage):
    assistant, credentials, snapshot = initialized_assistant()
    assistant.memory.load.return_value = replace(snapshot,
        state=replace(snapshot.state, booking_stage=stage))
    with pytest.raises(ValueError):
        assistant.confirm(credentials, expected_version=3, consent=True)
    assistant.memory.append.assert_not_called()
    assistant.llm.generate.assert_not_called()
