"""HTTP-контракт Supabase и валидация серверной границы без доступа к облаку."""

import base64
import hashlib
import json
import os
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import httpx

from config import ConfigError, SupabaseSettings, load_supabase_settings
from database.repositories import Repositories, SessionCredentials
from database.supabase_client import ConflictError, DatabaseError, SessionAccessError, SupabaseClient


KEY = "sb_secret_" + "x" * 32


class DatabaseTests(TestCase):
    def setUp(self):
        self.settings = SupabaseSettings("https://example.supabase.co", KEY)
        self.credentials = SessionCredentials(str(uuid4()), "a" * 43)

    def client(self, handler):
        return SupabaseClient(self.settings, httpx.MockTransport(handler))

    def test_secret_key_headers_and_readonly_health(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://example.supabase.co/rest/v1/rpc/legal_ai_health")
            self.assertEqual(request.headers["apikey"], KEY)
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(json.loads(request.content), {})
            return httpx.Response(200, json={"schema_version": 1, "rls_enabled": True, "public_access": False})
        with self.client(handler) as client:
            self.assertEqual(Repositories(client).health()["schema_version"], 1)
        self.assertNotIn(KEY, repr(self.settings))

    def test_legacy_service_role_header(self):
        payload = base64.urlsafe_b64encode(b'{"role":"service_role"}').decode().rstrip("=")
        key = "eyJhbGciOiJIUzI1NiJ9." + payload + ".signature"
        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer " + key)
            return httpx.Response(200, json={})
        with SupabaseClient(SupabaseSettings(self.settings.url, key), httpx.MockTransport(handler)) as client:
            client.rpc("health", {})

    def test_config_rejects_public_and_bad_keys(self):
        anon = base64.urlsafe_b64encode(b'{"role":"anon"}').decode().rstrip("=")
        for key in ("", "sb_publishable_" + "x" * 32, "not-a-key" * 4, "header." + anon + ".sig"):
            with self.subTest(key_type=key[:8]), self.assertRaises(ConfigError):
                SupabaseSettings(self.settings.url, key)

    def test_config_rejects_unsafe_urls(self):
        for url in ("", "http://example.com", "https://user:pw@example.com", "https://example.com/path",
                    "https://example.com?x=1", "https://[invalid"):
            with self.assertRaises(ConfigError):
                SupabaseSettings(url, KEY)

    def test_config_independent_of_llm(self):
        with patch.dict(os.environ, {"SUPABASE_URL": self.settings.url, "SUPABASE_KEY": KEY}, clear=True), \
                patch("config.load_dotenv"):
            self.assertEqual(load_supabase_settings().url, self.settings.url)

    def test_credential_secrets_hashed_and_masked(self):
        parameters = self.credentials.parameters()
        self.assertEqual(parameters["p_token_hash"], hashlib.sha256(b"a" * 43).hexdigest())
        self.assertNotIn(self.credentials.token, repr(self.credentials))
        self.assertNotIn(self.credentials.token, json.dumps(parameters))
        with self.client(lambda r: self.fail("No HTTP expected")) as client:
            a = Repositories(client).new_credentials()
            b = Repositories(client).new_credentials()
            self.assertNotEqual(a, b)
            self.assertEqual(len(a.token), 43)

    def test_session_and_messages_scoped_by_credentials(self):
        def handler(request):
            params = json.loads(request.content)
            self.assertEqual(params["p_session_id"], self.credentials.session_id)
            self.assertEqual(params["p_token_hash"], self.credentials.parameters()["p_token_hash"])
            if request.url.path.endswith("messages"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"id": self.credentials.session_id, "state": {}, "version": 0})
        with self.client(handler) as client:
            repo = Repositories(client)
            repo.create_session(self.credentials)
            repo.get_session(self.credentials)
            self.assertEqual(repo.messages(self.credentials), [])

    def test_safe_errors_and_no_retry(self):
        for status, code, exception in ((403, "42501", DatabaseError), (401, "auth", DatabaseError),
                                      (404, "PGRST202", DatabaseError), (400, "P0002", SessionAccessError),
                                      (409, "23505", ConflictError), (409, "40001", ConflictError),
                                      (429, "rate", DatabaseError), (500, "error", DatabaseError),
                                      (302, "redirect", DatabaseError)):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={"code": code, "message": KEY + " private phone"})
            with self.subTest(status=status, code=code), self.client(handler) as client:
                with self.assertRaises(exception) as error:
                    client.rpc("health", {})
                self.assertNotIn(KEY, str(error.exception))
                self.assertNotIn("private phone", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_network_and_bad_json(self):
        for kind in (httpx.ReadTimeout, httpx.ConnectError):
            def handler(request):
                raise kind(KEY, request=request)
            with self.client(handler) as client:
                with self.assertRaises(DatabaseError) as error:
                    client.rpc("health", {})
                self.assertNotIn(KEY, str(error.exception))
                self.assertIsNone(error.exception.__context__)
        with self.client(lambda r: httpx.Response(200, text="invalid")) as client:
            with self.assertRaises(DatabaseError):
                client.rpc("health", {})

    def test_unsafe_schema_health_rejected(self):
        for result in ([], {}, {"schema_version": 1, "rls_enabled": False, "public_access": False},
                       {"schema_version": 1, "rls_enabled": True, "public_access": True}):
            with self.client(lambda r: httpx.Response(200, json=result)) as client:
                with self.assertRaises(DatabaseError):
                    Repositories(client).health()

    def test_invalid_input_never_reaches_http(self):
        with self.client(lambda r: self.fail("No HTTP expected")) as client:
            repo = Repositories(client)
            for role, content in (("system", "rules"), ("user", " "), ("assistant", "x" * 16001)):
                with self.assertRaises(ValueError):
                    repo.append_message(self.credentials, str(uuid4()), role, content)
            for limit in (0, 101, True):
                with self.assertRaises(ValueError):
                    repo.messages(self.credentials, limit)
            for state in ({"x": float("nan")}, {"x": "x" * 8001}, {"x": object()}):
                with self.assertRaises(ValueError):
                    repo.update_state(self.credentials, 0, state)
            with self.assertRaises(ValueError):
                client.rpc("../leads", {})
            with self.assertRaises(ValueError):
                repo.get_session(SessionCredentials(self.credentials.session_id, "short"))

    def test_lead_consent_and_phone_validation(self):
        with self.client(lambda r: self.fail("No HTTP expected")) as client:
            for consent, phone in ((False, "+70000000000"), (1, "+70000000000"), (True, "bad")):
                with self.assertRaises(ValueError):
                    Repositories(client).create_lead(self.credentials, idempotency_key=str(uuid4()),
                        name="Тест", phone=phone, legal_topic="Тест", description="Синтетический тест",
                        consent=consent, consent_version="test-v1")

    def test_lead_only_confirmed_by_returned_id(self):
        key = str(uuid4())
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["p_idempotency_key"], key)
            self.assertTrue(payload["p_consent"])
            return httpx.Response(200, json={"id": key, "created_at": "2026-10-16T10:00:00Z", "duplicate": False})
        with self.client(handler) as client:
            result = Repositories(client).create_lead(self.credentials, idempotency_key=key,
                name="Тест", phone="+70000000000", legal_topic="Тест", description="Синтетический тест",
                consent=True, consent_version="test-v1")
            self.assertEqual(result["id"], key)
        with self.client(lambda r: httpx.Response(200, json={})) as client:
            with self.assertRaises(DatabaseError):
                Repositories(client).create_lead(self.credentials, idempotency_key=key,
                    name="Тест", phone="+70000000000", legal_topic="Тест", description="Тест",
                    consent=True, consent_version="test-v1")

    def test_deletions_are_scoped_and_confirmed(self):
        def handler(request):
            self.assertEqual(json.loads(request.content), self.credentials.parameters())
            if request.url.path.endswith("clear_history"):
                result = {"deleted_messages": 2}
            elif request.url.path.endswith("delete_leads"):
                result = {"deleted_leads": 1}
            else:
                result = {"deleted": True}
            return httpx.Response(200, json=result)
        with self.client(handler) as client:
            repo = Repositories(client)
            self.assertEqual(repo.clear_history(self.credentials)["deleted_messages"], 2)
            self.assertEqual(repo.delete_leads(self.credentials)["deleted_leads"], 1)
            self.assertTrue(repo.delete_session(self.credentials)["deleted"])
