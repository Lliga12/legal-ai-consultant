"""Telegram/worker: никакие внешние сообщения в этих тестах не отправляются."""

from dataclasses import replace
import json
import os
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from config import ConfigError, TelegramSettings, load_telegram_settings
from database.repositories import Repositories
from database.supabase_client import DatabaseError
from tools.notification_worker import NotificationWorker
from tools.telegram_tools import TelegramClient, TelegramError, format_notification, send_telegram_notification
from tools.telegram_setup import discover_chats


TOKEN = "123456789:" + "x"*35


class TelegramTests(TestCase):
    def setUp(self):
        self.settings = TelegramSettings(TOKEN,"123456")
        self.lead = {"id":str(uuid4()),"name":"Тестовое Имя","phone":"+70000001234",
            "legal_topic":"Алименты","description":"Секретное описание", "created_at":"2026-10-16T10:00:00.12345Z"}

    def client(self, handler):
        return TelegramClient(self.settings,httpx.MockTransport(handler))

    def success(self, request):
        return httpx.Response(200,json={"ok":True,"result":{"message_id":10,"chat":{"id":123456}}})

    def test_minimal_notification_and_time(self):
        text = format_notification(self.lead)
        self.assertIn("Тестовое",text)
        self.assertNotIn("Тестовое Имя",text)
        self.assertIn("Контакт: +7 *** *** 12 34",text)
        self.assertIn("16.10.2026 13:00",text)
        self.assertIn("Взыскание алиментов",text)
        self.assertNotIn("Секретное описание",text)
        self.assertNotIn(self.lead["phone"],text)
        self.assertIn(self.lead["phone"],format_notification(self.lead,include_contact=True))

    def test_freeform_topic_and_invalid_name_not_forwarded(self):
        self.lead["legal_topic"] = "Позвоните +70000001234, private@example.com"
        self.lead["name"] = "private@example.com"
        text = format_notification(self.lead)
        self.assertNotIn("private@example.com",text)
        self.assertIn("Имя: Клиент",text)
        self.assertIn("Общий юридический вопрос",text)

    def test_send_payload_and_protected_content(self):
        def handler(request):
            self.assertTrue(str(request.url).endswith("/sendMessage"))
            body = json.loads(request.content)
            self.assertEqual(body["chat_id"],"123456")
            self.assertTrue(body["protect_content"])
            self.assertNotIn("parse_mode",body)
            self.assertNotIn("allow_paid_broadcast",body)
            return self.success(request)
        with self.client(handler) as client:
            self.assertEqual(send_telegram_notification(self.lead,client=client),10)

    def test_http_logs_redact_bot_token(self):
        with self.client(self.success) as client, self.assertLogs("httpx",level="INFO") as logs:
            client.send_telegram_notification(self.lead)
        self.assertNotIn(TOKEN," ".join(logs.output))
        self.assertIn("REDACTED"," ".join(logs.output))
        self.assertNotIn(TOKEN,repr(self.settings))

    def test_bot_and_chat_check_without_send(self):
        methods = []
        def handler(request):
            methods.append(request.url.path.rsplit("/",1)[-1])
            result = {"is_bot":True,"id":111} if methods[-1] == "getMe" else {"id":123456}
            return httpx.Response(200,json={"ok":True,"result":result})
        with self.client(handler) as client:
            client.check()
        self.assertEqual(methods,["getMe","getChat"])

    def test_api_errors_are_safe(self):
        for code in (400,401,403,429,500):
            def handler(request):
                return httpx.Response(code,json={"ok":False,"error_code":code,"description":TOKEN,
                    "parameters":{"retry_after":90}})
            with self.client(handler) as client:
                with self.assertRaises(TelegramError) as error:
                    client.send_telegram_notification(self.lead)
                self.assertNotIn(TOKEN,str(error.exception))
                self.assertFalse(error.exception.uncertain)
                self.assertEqual(error.exception.retryable,code in {429,500})
                if code == 429:
                    self.assertEqual(error.exception.retry_after,90)

    def test_timeout_and_response_ambiguity(self):
        def timeout(request):
            raise httpx.ReadTimeout(TOKEN,request=request)
        with self.client(timeout) as client:
            with self.assertRaises(TelegramError) as error:
                client.send_telegram_notification(self.lead)
            self.assertTrue(error.exception.uncertain)
            self.assertIsNone(error.exception.__context__)
        responses = [httpx.Response(200,text="bad"),httpx.Response(200,json=[]),
            httpx.Response(200,json={"ok":True,"result":{}}),
            httpx.Response(200,json={"ok":True,"result":{"message_id":1,"chat":{"id":999}}})]
        for response in responses:
            with self.client(lambda r: response) as client:
                with self.assertRaises(TelegramError) as error:
                    client.send_telegram_notification(self.lead)
                self.assertTrue(error.exception.uncertain)

    def test_connect_failure_is_retryable(self):
        def handler(request):
            raise httpx.ConnectError(TOKEN,request=request)
        with self.client(handler) as client:
            with self.assertRaises(TelegramError) as error:
                client.send_telegram_notification(self.lead)
            self.assertTrue(error.exception.retryable)
            self.assertFalse(error.exception.uncertain)

    def test_oversized_or_invalid_retry_after_halts(self):
        for value in (True,-1,604801,"90"):
            with self.client(lambda r: httpx.Response(429,json={"ok":False,"error_code":429,
                    "parameters":{"retry_after":value}})) as client:
                with self.assertRaises(TelegramError) as error:
                    client.send_telegram_notification(self.lead)
                self.assertFalse(error.exception.retryable)

    def test_telegram_config_validation(self):
        for changes in ({"token":"bad"},{"chat_id":"@username"},{"chat_id":"0"},
                        {"timeout":0},{"max_attempts":11},{"lease_seconds":30}):
            with self.assertRaises(ConfigError):
                replace(self.settings,**changes)
        env = {"TELEGRAM_BOT_TOKEN":TOKEN,"TELEGRAM_CHAT_ID":"123456"}
        with patch.dict(os.environ,env,clear=True),patch("config.load_dotenv"):
            self.assertFalse(load_telegram_settings().include_contact)

    def test_chat_discovery_only_returns_ids_and_types(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertNotIn("offset",payload)
            self.assertTrue(str(request.url).endswith("/getUpdates"))
            return httpx.Response(200,json={"ok":True,"result":[
                {"message":{"text":"private content","chat":{"id":123456,"type":"private","first_name":"Private"}}},
                {"message":{"chat":{"id":-123456,"type":"group"}}}]})
        result = discover_chats(TOKEN,httpx.MockTransport(handler))
        self.assertEqual(result,{123456:"private",-123456:"group"})
        self.assertNotIn("private content",str(result))

    def test_chat_discovery_errors_do_not_expose_token(self):
        def handler(request):
            raise httpx.ConnectError(TOKEN,request=request)
        with self.assertRaises(ValueError) as error:
            discover_chats(TOKEN,httpx.MockTransport(handler))
        self.assertNotIn(TOKEN,str(error.exception))


class WorkerTests(TestCase):
    def setUp(self):
        self.repo = Mock(spec=Repositories)
        self.telegram = Mock(spec=TelegramClient)
        self.telegram.settings = TelegramSettings(TOKEN,"123456")
        self.telegram.send_telegram_notification.return_value = 10
        self.job = {"id":str(uuid4()),"claim_token":str(uuid4()),"attempts":1}
        self.repo.claim_notification.return_value = self.job
        self.repo.notification_current.return_value = {"id":str(uuid4())}
        self.repo.finish_notification.return_value = True
        self.worker = NotificationWorker(self.repo,self.telegram)

    def test_delivery_recorded_after_send(self):
        self.assertEqual(self.worker.run_once().status,"sent")
        self.repo.finish_notification.assert_called_once_with(self.job["id"],self.job["claim_token"],
            sent=True,message_id=10,error_code=None,retry_delay=None,cooldown_delay=None)

    def test_empty_or_deleted_jobs_do_not_send(self):
        self.repo.claim_notification.return_value = None
        self.assertEqual(self.worker.run_once().status,"idle")
        self.repo.claim_notification.return_value = self.job
        self.repo.notification_current.return_value = None
        self.assertEqual(self.worker.run_once().status,"cancelled_or_expired")
        self.telegram.send_telegram_notification.assert_not_called()

    def test_known_failure_schedules_retry(self):
        self.telegram.send_telegram_notification.side_effect = TelegramError("api_429",retryable=True,retry_after=90)
        self.assertEqual(self.worker.run_once().status,"retry_scheduled")
        self.assertEqual(self.repo.finish_notification.call_args.kwargs["retry_delay"],90)
        self.assertEqual(self.repo.finish_notification.call_args.kwargs["cooldown_delay"],90)

    def test_uncertain_or_permanent_failure_halts(self):
        for error in (TelegramError("delivery_unknown",uncertain=True),TelegramError("api_403")):
            self.telegram.send_telegram_notification.side_effect = error
            self.assertEqual(self.worker.run_once().status,"needs_attention")
            self.assertIsNone(self.repo.finish_notification.call_args.kwargs["retry_delay"])

    def test_last_attempt_halts(self):
        self.job["attempts"] = 5
        self.telegram.send_telegram_notification.side_effect = TelegramError("api_500",retryable=True)
        self.assertEqual(self.worker.run_once().status,"needs_attention")
        self.assertIsNone(self.repo.finish_notification.call_args.kwargs["retry_delay"])

    def test_db_error_after_send_never_repeats_send(self):
        self.repo.finish_notification.side_effect = DatabaseError("database offline")
        self.assertEqual(self.worker.run_once().status,"unconfirmed")
        self.assertEqual(self.telegram.send_telegram_notification.call_count,1)

    def test_lost_fencing_token_does_not_claim_success(self):
        self.repo.finish_notification.return_value = False
        self.assertEqual(self.worker.run_once().status,"unconfirmed")

    def test_batch_stops_on_unconfirmed_delivery(self):
        self.repo.finish_notification.side_effect = DatabaseError("offline")
        self.assertEqual(len(self.worker.run_batch()),1)
        self.assertEqual(self.telegram.send_telegram_notification.call_count,1)

    def test_repository_rejects_sensitive_error_text(self):
        repo = Repositories(Mock())
        with self.assertRaises(ValueError):
            repo.finish_notification(self.job["id"],self.job["claim_token"],sent=False,error_code=TOKEN)
