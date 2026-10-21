"""Voice security, spend limits and draft-only tool boundary; no paid API calls."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from config import WebSettings
from database.supabase_client import ConflictError
from tests.test_assistant import initialized_assistant
from ui.voice import VoiceService, VoiceSettings, install_routes


def service():
    assistant, credentials, snapshot = initialized_assistant()
    assistant.memory.repositories = Mock()
    voice = VoiceService(assistant, lambda: VoiceSettings("private-test-key", "b1testfolder00", "aiprtestprompt00"))
    return voice, credentials, snapshot


def app_for(voice, required=True):
    app = FastAPI()
    login = SimpleNamespace(cookie_id="test", tokens={"good-token": "owner"})
    install_routes(app, voice, lambda: login, required)
    return app


def test_ticket_consent_version_expiry_and_single_use():
    voice, credentials, snapshot = service()
    for consent in (False, None, "да", 1):
        with pytest.raises(ValueError):
            voice.issue(consent, credentials, snapshot.version)
    with pytest.raises(ConflictError):
        voice.issue(True, credentials, snapshot.version - 1)
    token = voice.issue(True, credentials, snapshot.version)
    voice._tickets[token].expires = 0
    with pytest.raises(ValueError):
        voice.take(token)
    token = voice.issue(True, credentials, snapshot.version)
    voice.take(token)
    with pytest.raises(ValueError):
        voice.take(token)
    other = voice.issue(True, credentials, snapshot.version)
    with pytest.raises(ValueError):
        voice.take(other)
    voice.active = False
    voice._starts = [__import__('time').monotonic()] * 10
    with pytest.raises(ValueError):
        voice.take(voice.issue(True, credentials, snapshot.version))


def test_draft_tool_cannot_grant_consent_or_submit_lead():
    voice, credentials, snapshot = service()
    ticket = voice.take(voice.issue(True, credentials, snapshot.version))
    repo = voice.assistant.memory.repositories
    request_id = str(uuid4())
    repo.prepare_lead.return_value = {"request_id": request_id, "session_id": credentials.session_id,
        "session_version": 4, "name": "Тест", "phone": "+70000000000", "legal_topic": "Потребительский спор",
        "description": "Чайник не включается", "consent_version": "test-lead-v1"}
    voice.assistant.memory.save_state.return_value = replace(snapshot, version=5)
    args = {"name": "Тест", "phone": "+70000000000", "legal_topic": "Потребительский спор", "description": "Чайник не включается"}
    result = voice.prepare(ticket, json.dumps(args))
    assert result["ok"] and ticket.version == 5
    state = voice.assistant.memory.save_state.call_args.kwargs["state"]
    assert state.lead_request_id == request_id and state.booking_stage.value == "consent"
    assert state.booking_description == "Чайник не включается"
    assert not voice.prepare(ticket, json.dumps(args | {"consent": True}))["ok"]
    repo.submit_lead.assert_not_called()
    repo.lead_consent.assert_not_called()


def test_stale_draft_never_writes():
    voice, credentials, snapshot = service()
    ticket = voice.take(voice.issue(True, credentials, snapshot.version))
    voice.assistant.memory.load.return_value = replace(snapshot, version=4)
    assert not voice.prepare(ticket, json.dumps({"name": "Тест", "phone": "+70000000000",
        "legal_topic": "Спор", "description": "Тест"}))["ok"]
    voice.assistant.memory.repositories.prepare_lead.assert_not_called()


def test_voice_requires_login_and_same_origin_before_upstream():
    voice, _, _ = service()
    with TestClient(app_for(voice)) as client:
        assert client.get("/voice/").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/voice/ws", headers={"origin": "http://testserver"}):
                pass
        client.cookies.set("access-token-test", "good-token")
        assert client.get("/voice/").status_code == 200
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/voice/ws", headers={"origin": "https://evil.example"}):
                pass
        assert not voice.active and not voice._starts


class FakeUpstream:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    async def send(self, raw):
        e = json.loads(raw)
        self.sent.append(e)
        if e['type'] == 'session.update':
            await self.queue.put(json.dumps({'type': 'session.updated', 'session': {'secret': 'must-not-leak'}}))
        if e['type'] == 'response.create':
            await self.queue.put(json.dumps({'type': 'response.created'}))


def test_relay_restricts_browser_protocol_and_closes_paid_socket():
    voice, credentials, snapshot = service()
    token = voice.issue(True, credentials, snapshot.version)
    upstream = FakeUpstream()
    with patch('websockets.asyncio.client.connect', return_value=upstream) as connect:
        with TestClient(app_for(voice, required=False)) as client:
            with client.websocket_connect('/voice/ws', headers={'origin': 'http://testserver'}) as socket:
                socket.send_json({'ticket': token})
                assert socket.receive_json() == {'type': 'ready'}
                assert socket.receive_json() == {'type': 'response.created'}
                # The browser cannot change models, tools, instructions or consent.
                socket.send_json({'type': 'session.update', 'session': {'tools': []}})
                assert socket.receive_json()['type'] == 'notice'
        assert upstream.closed and not voice.active
        assert len([e for e in upstream.sent if e['type'] == 'session.update']) == 2
        assert connect.call_args.kwargs['additional_headers']['Authorization'] == 'Api-Key private-test-key'


def test_mounted_app_preserves_gradio_login_and_hides_provider_secrets():
    from ui.gradio_app import Runtime, create_server
    settings = WebSettings(7860, 'owner', 'a-long-local-test-password')
    with TestClient(create_server(Runtime(issue='offline test'), settings)) as client:
        assert client.get('/voice/').status_code == 401
        assert client.get('/config').status_code == 401
        assert client.post('/login', data={'username': 'owner', 'password': 'wrong'}).status_code == 400
        assert client.post('/login', data={'username': 'owner', 'password': settings.password}).status_code == 200
        assert client.get('/voice/').status_code == 200
        config = client.get('/config')
        assert config.status_code == 200
        assert settings.password not in config.text
        assert 'Начать разговор' in config.text


def test_server_duration_limit_closes_upstream_without_browser_cooperation():
    voice, credentials, snapshot = service()
    voice.settings_loader = lambda: VoiceSettings('private-test-key', 'b1testfolder00', 'aiprtestprompt00', seconds=.01)
    token = voice.issue(True, credentials, snapshot.version)
    upstream = FakeUpstream()
    with patch('websockets.asyncio.client.connect', return_value=upstream):
        with TestClient(app_for(voice, required=False)) as client:
            with client.websocket_connect('/voice/ws', headers={'origin': 'http://testserver'}) as socket:
                socket.send_json({'ticket': token})
                assert socket.receive_json()['type'] == 'ready'
                assert socket.receive_json()['type'] == 'response.created'
                assert socket.receive_json()['type'] == 'notice'
        assert upstream.closed and not voice.active


def test_raw_provider_error_and_settings_are_never_relayed():
    voice, credentials, snapshot = service()
    upstream = FakeUpstream()
    async def fail(raw):
        await upstream.queue.put(json.dumps({'type': 'error', 'error': {'message': 'private-test-key'}}))
    upstream.send = fail
    with patch('websockets.asyncio.client.connect', return_value=upstream):
        with TestClient(app_for(voice, required=False)) as client:
            with client.websocket_connect('/voice/ws', headers={'origin': 'http://testserver'}) as socket:
                socket.send_json({'ticket': voice.issue(True, credentials, snapshot.version)})
                event = socket.receive_json()
                assert event['type'] == 'notice' and 'private-test-key' not in json.dumps(event)
        assert upstream.closed and not voice.active
