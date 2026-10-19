"""Проверка границы Memory. Постоянное хранение дополнительно проверяет PostgreSQL-runner."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from core.memory import ConversationState, HistoryConsentRequired, Memory, MemoryDataError
from core.router import BookingStage, Intent
from database.repositories import Repositories, SessionCredentials
from database.supabase_client import ConflictError, DatabaseError


class MemoryTests(TestCase):
    def setUp(self):
        self.credentials = SessionCredentials(str(uuid4()), "a" * 43)
        self.repo = Mock(spec=Repositories)
        self.memory = Memory(self.repo)
        self.state = ConversationState(
            legal_topic="Взыскание алиментов", facts=("Есть решение суда",), client_name="Тестовое имя",
            booking_stage=BookingStage.NAME, history_notice_version="test-notice-v1",
            history_consent_at="2026-10-16T10:00:00+00:00")
        self.data = {"session": {"id": self.credentials.session_id, "version": 2,
                                "state": self.state.to_dict()}, "messages": []}
        self.repo.memory_snapshot.return_value = deepcopy(self.data)

    def message_row(self, sequence=1, role="user", content="Хочу записаться"):
        return {"id": str(uuid4()), "session_id": self.credentials.session_id,
                "sequence": sequence, "role": role, "content": content}

    def test_load_restores_typed_state_and_history(self):
        self.data["messages"] = [self.message_row(), self.message_row(2, "assistant", "Как вас зовут?")]
        self.repo.memory_snapshot.return_value = self.data
        snapshot = self.memory.load(self.credentials)
        self.assertEqual(snapshot.state, self.state)
        self.assertEqual(snapshot.messages[-1].content, "Как вас зовут?")
        self.assertEqual(snapshot.version, 2)
        self.repo.memory_snapshot.assert_called_once_with(self.credentials, 40)

    def test_no_private_data_in_snapshot_repr(self):
        self.data["messages"] = [self.message_row(content="Частное сообщение")]
        self.repo.memory_snapshot.return_value = self.data
        snapshot = self.memory.load(self.credentials)
        self.assertNotIn("Частное сообщение", repr(snapshot))
        self.assertNotIn("Тестовое имя", repr(snapshot.state))
        self.assertNotIn(self.credentials.token, repr(snapshot))

    def test_refused_history_consent_never_calls_database(self):
        for consent in (False, None, 1, "да"):
            with self.assertRaises(HistoryConsentRequired):
                self.memory.initialize(self.credentials, history_consent=consent, notice_version="test-v1")
        self.assertEqual(self.repo.mock_calls, [])

    def test_initialize_existing_notice_preserves_timestamp(self):
        snapshot = self.memory.initialize(self.credentials, history_consent=True, notice_version="test-notice-v1")
        self.assertEqual(snapshot.state.history_consent_at, self.state.history_consent_at)
        self.repo.update_state.assert_not_called()

    def test_initialize_new_notice_keeps_existing_context(self):
        self.memory.initialize(self.credentials, history_consent=True, notice_version="test-notice-v2")
        _, version, state = self.repo.update_state.call_args.args
        self.assertEqual(version, 2)
        self.assertEqual(state["history_notice_version"], "test-notice-v2")
        self.assertEqual(state["facts"], list(self.state.facts))
        self.assertTrue(state["history_consent_at"].endswith("+00:00"))

    def test_empty_state_is_idle_and_without_consent(self):
        self.data["session"]["state"] = {}
        self.repo.memory_snapshot.return_value = self.data
        snapshot = self.memory.load(self.credentials)
        self.assertEqual(snapshot.state.booking_stage, BookingStage.IDLE)
        self.assertFalse(snapshot.state.history_allowed)

    def test_append_uses_message_id_version_and_credentials(self):
        self.repo.memory_append.return_value = self.data
        message_id = str(uuid4())
        self.memory.append(self.credentials, message_id=message_id, role="user", content="Тест", expected_version=2)
        self.repo.memory_append.assert_called_once_with(self.credentials, message_id=message_id,
            role="user", content="Тест", expected_version=2, limit=40)

    def test_save_state_uses_compare_and_swap(self):
        updated = replace(self.state, client_name="Другое тестовое имя", booking_stage=BookingStage.PHONE)
        self.memory.save_state(self.credentials, expected_version=2, state=updated)
        self.repo.update_state.assert_called_once_with(self.credentials, 2, updated.to_dict())

    def test_conflict_is_not_silently_retried(self):
        self.repo.update_state.side_effect = ConflictError("state conflict")
        with self.assertRaises(ConflictError):
            self.memory.save_state(self.credentials, expected_version=1, state=self.state)
        self.assertEqual(self.repo.update_state.call_count, 1)

    def test_save_state_cannot_grant_consent(self):
        changed = replace(self.state, history_notice_version="forged-version")
        with self.assertRaises(ValueError):
            self.memory.save_state(self.credentials, expected_version=2, state=changed)
        self.repo.update_state.assert_not_called()

    def test_save_without_history_consent_fails(self):
        self.data["session"]["state"] = {}
        self.repo.memory_snapshot.return_value = self.data
        with self.assertRaises(HistoryConsentRequired):
            self.memory.save_state(self.credentials, expected_version=0, state=self.state)
        self.repo.update_state.assert_not_called()

    def test_router_uses_persisted_stage_and_topic(self):
        result, snapshot = self.memory.route(self.credentials, "Тестовое имя")
        self.assertEqual(result.intent, Intent.BOOKING)
        self.assertEqual(result.legal_topic, "Взыскание алиментов")
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(self.memory.route(self.credentials, "Сколько стоит?")[0].intent, Intent.PRICE)

    def test_current_topic_overrides_saved_topic(self):
        result, _ = self.memory.route(self.credentials, "Как вернуть товар?")
        self.assertEqual(result.legal_topic, "Защита прав потребителей")

    def test_invalid_or_foreign_context_rejected(self):
        variants = []
        for changes in ({"id": str(uuid4())}, {"version": True}, {"version": -1},
                        {"token_hash": "private"}, {"state": {"booking_stage": "phone"}}):
            data = deepcopy(self.data)
            data["session"].update(changes)
            variants.append(data)
        for row in (self.message_row(role="system"), self.message_row(content=" ")):
            data = deepcopy(self.data)
            data["messages"] = [row]
            variants.append(data)
        data = deepcopy(self.data)
        row = self.message_row()
        row["session_id"] = str(uuid4())
        data["messages"] = [row]
        variants.append(data)
        variants.extend(({}, {"session": None, "messages": []}))
        for data in variants:
            self.repo.memory_snapshot.return_value = data
            with self.assertRaises(MemoryDataError):
                self.memory.load(self.credentials)

    def test_unordered_duplicate_and_oversized_history_rejected(self):
        row = self.message_row()
        for rows in ([self.message_row(2), self.message_row(1)], [row, row],
                     [self.message_row(i + 1) for i in range(41)]):
            self.data["messages"] = rows
            self.repo.memory_snapshot.return_value = self.data
            with self.assertRaises(MemoryDataError):
                self.memory.load(self.credentials)

    def test_database_outage_does_not_return_empty_memory(self):
        self.repo.memory_snapshot.side_effect = DatabaseError("connection failed")
        with self.assertRaises(DatabaseError):
            self.memory.load(self.credentials)

    def test_clear_and_delete_are_scoped_to_credentials(self):
        self.repo.clear_history.return_value = {"deleted_messages": 3}
        self.assertEqual(self.memory.clear_history(self.credentials), 3)
        self.repo.clear_history.assert_called_once_with(self.credentials)
        self.memory.delete_session(self.credentials)
        self.repo.delete_session.assert_called_once_with(self.credentials)

    def test_typed_state_validation_and_immutability(self):
        for changes in ({"booking_stage": "phone"}, {"facts": ["Тест"]},
                        {"facts": ("Тест",) * 21}, {"client_name": "x" * 101},
                        {"history_consent_at": "2026-10-16T10:00:00"},
                        {"history_consent_at": None}, {"facts": ("я" * 500,) * 20}):
            with self.assertRaises(ValueError):
                replace(self.state, **changes)
        with self.assertRaises(FrozenInstanceError):
            self.state.client_name = "Новое имя"
        with self.assertRaises(ValueError):
            ConversationState.from_dict({"consent": True})
        with self.assertRaises(ValueError):
            Memory(self.repo, history_limit=0)
