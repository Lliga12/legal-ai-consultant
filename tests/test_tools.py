"""Граница Tool Calling: валидация, доверенные параметры и честный результат."""

from unittest import TestCase
from unittest.mock import Mock
from uuid import uuid4

from database.repositories import Repositories, SessionCredentials
from database.supabase_client import (ConflictError, DatabaseError, DataValidationError,
                                       SessionAccessError)
from tools.lead_tools import LeadStatus, LeadTools, normalize_phone


class LeadToolTests(TestCase):
    def setUp(self):
        self.repo = Mock(spec=Repositories)
        self.credentials = SessionCredentials(str(uuid4()), "a" * 43)
        self.tools = LeadTools(self.repo, self.credentials, "synthetic-consent-v1")
        self.request_id = str(uuid4())
        self.arguments = {"name":"Тестовое имя", "phone":"8 (000) 000-00-00", "legal_topic":"Тест",
                          "description":"Синтетическое описание", "session_id":self.credentials.session_id,
                          "consent":True}
        self.lead_id = str(uuid4())
        self.repo.submit_lead.return_value = {"id":self.lead_id,"duplicate":False,
            "created_at":"2026-10-16T12:00:00Z","notification_status":"pending"}
        self.draft = {"request_id":self.request_id,"session_id":self.credentials.session_id,
            "session_version":2,"name":"Тестовое имя","phone":"+70000000000","legal_topic":"Тест",
            "description":"Синтетическое описание","consent_version":"synthetic-consent-v1",
            "consent_at":None,"lead_id":None}

    def call(self, **changes):
        return self.tools.create_lead(**{**self.arguments, **changes}, request_id=self.request_id)

    def test_phone_normalization(self):
        for value in ("8 (000) 000-00-00", "70000000000", "+7 000 000 00 00", "0000000000"):
            self.assertEqual(normalize_phone(value), "+70000000000")
        for value in ("+1 000 000 0000", "phone", "+7１２３４５６７８９０", "123", None, "7" * 41):
            with self.assertRaises(ValueError):
                normalize_phone(value)

    def test_postgres_timestamps_with_trimmed_fractional_seconds(self):
        for fraction in ("", ".1", ".12", ".123", ".1234", ".12345", ".123456"):
            for zone in ("Z", "+00:00", "+03:00"):
                stamp = "2026-10-16T12:00:00" + fraction + zone
                self.repo.submit_lead.return_value["created_at"] = stamp
                self.assertTrue(self.call().success, stamp)
                self.draft["consent_at"] = stamp
                self.repo.lead_request.return_value = self.draft
                self.assertEqual(self.tools.get_request(self.request_id).consent_at, stamp)

    def test_valid_create_returns_db_id_and_outbox_state(self):
        result = self.call()
        self.assertTrue(result.success)
        self.assertEqual(result.status, LeadStatus.CREATED)
        self.assertEqual(result.lead_id, self.lead_id)
        self.assertEqual(result.notification_status, "pending")
        data = self.repo.submit_lead.call_args.args[2]
        self.assertEqual(data["phone"], "+70000000000")
        self.assertEqual(data["consent_version"], "synthetic-consent-v1")
        self.assertNotIn("отправлено", result.message.casefold())

    def test_duplicate_returns_existing_id(self):
        self.repo.submit_lead.return_value["duplicate"] = True
        result = self.call()
        self.assertEqual(result.status, LeadStatus.ALREADY_EXISTS)
        self.assertEqual(result.lead_id, self.lead_id)

    def test_refusal_does_not_call_database(self):
        for consent in (False, None, "true", 1):
            result = self.call(consent=consent)
            self.assertEqual(result.status, LeadStatus.CONSENT_REQUIRED)
            self.assertFalse(result.success)
        self.repo.submit_lead.assert_not_called()

    def test_wrong_session_never_reaches_database(self):
        self.assertEqual(self.call(session_id=str(uuid4())).status, LeadStatus.SESSION_UNAVAILABLE)
        self.repo.submit_lead.assert_not_called()

    def test_invalid_fields_no_database_call(self):
        for changes in ({"name":"<script>"},{"name":""},{"phone":"bad"},
                        {"legal_topic":"x"*201},{"description":"x"*2001}, {"description":"bad\x00value"}):
            self.assertEqual(self.call(**changes).status, LeadStatus.VALIDATION_ERROR)
        self.repo.submit_lead.assert_not_called()

    def test_errors_are_safe_and_never_successful(self):
        cases = ((DataValidationError,LeadStatus.CONSENT_REQUIRED),(ConflictError,LeadStatus.CONFLICT),
                 (SessionAccessError,LeadStatus.SESSION_UNAVAILABLE),(DatabaseError,LeadStatus.UNCONFIRMED))
        for error, status in cases:
            self.repo.submit_lead.side_effect = error("SECRET +70000000000")
            result = self.call()
            self.assertEqual(result.status, status)
            self.assertFalse(result.success)
            self.assertIsNone(result.lead_id)
            self.assertNotIn("SECRET", repr(result))
            self.assertNotIn("+70000000000", repr(result))

    def test_malformed_success_response_is_unconfirmed(self):
        for data in ({}, {"id":"not-uuid"}, {"id":self.lead_id,"duplicate":False,"created_at":"bad"},
                     {"id":self.lead_id,"duplicate":False,"created_at":"2026-10-16T12:00:00Z",
                      "notification_status":"delivered_by_magic"}):
            self.repo.submit_lead.return_value = data
            self.assertEqual(self.call().status, LeadStatus.UNCONFIRMED)

    def test_failed_notification_does_not_erase_saved_lead(self):
        self.repo.submit_lead.return_value["notification_status"] = "failed"
        result = self.call()
        self.assertTrue(result.success)
        self.assertEqual(result.lead_id, self.lead_id)
        self.assertEqual(result.notification_status, "failed")

    def test_prepare_normalizes_before_persistence(self):
        self.repo.prepare_lead.return_value = self.draft
        draft = self.tools.prepare("Тестовое имя","8 (000) 000-00-00","Тест","Синтетическое описание",
                                   expected_version=1)
        self.assertEqual(draft.request_id,self.request_id)
        self.assertNotIn("Тестовое имя",repr(draft))
        self.assertNotIn("+70000000000",repr(draft))
        self.assertEqual(self.repo.prepare_lead.call_args.args[2]["phone"],"+70000000000")

    def test_record_consent_requires_actual_boolean_and_version(self):
        self.repo.lead_consent.return_value = self.draft
        self.tools.record_consent(self.request_id, consent=False, expected_version=2)
        self.repo.lead_consent.assert_called_once_with(self.credentials,self.request_id,2,False)
        with self.assertRaises(ValueError):
            self.tools.record_consent(self.request_id, consent="true", expected_version=2)

    def test_foreign_draft_rejected(self):
        self.draft["session_id"] = str(uuid4())
        self.repo.lead_request.return_value = self.draft
        with self.assertRaises(DatabaseError):
            self.tools.get_request(self.request_id)

    def test_tool_dispatch_whitelist(self):
        for tool_name, arguments in (("record_consent",self.arguments),("delete_session",{}),
                                      ("create_lead",{**self.arguments,"consent_version":"forged"}),
                                      ("create_lead",{**self.arguments,"request_id":str(uuid4())})):
            result = self.tools.dispatch(tool_name, arguments, request_id=self.request_id)
            self.assertEqual(result.status,LeadStatus.VALIDATION_ERROR)
        self.repo.submit_lead.assert_not_called()

    def test_dispatch_uses_server_bound_request_id(self):
        self.tools.dispatch("create_lead",self.arguments,request_id=self.request_id)
        self.assertEqual(self.repo.submit_lead.call_args.args[1],self.request_id)

    def test_repo_rejects_credential_overrides(self):
        repo = Repositories(Mock())
        data = {key:self.draft[key] for key in ("name","phone","legal_topic","description","consent_version")}
        for extra in ({"session_id":str(uuid4())},{"token_hash":"forged"},{"consent":True}):
            with self.assertRaises(ValueError):
                repo.submit_lead(self.credentials,self.request_id,{**data,**extra})

    def test_health_check_contract(self):
        client = Mock()
        client.rpc.return_value = {"tools_version":1,"rpc_ready":True}
        Repositories(client).tools_health()
        client.rpc.return_value = {"tools_version":1,"rpc_ready":False}
        with self.assertRaises(DatabaseError):
            Repositories(client).tools_health()
