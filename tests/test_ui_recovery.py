"""Recover a voice-updated draft without destroying the form or implying consent."""

from unittest.mock import Mock, patch

import gradio as gr

from database.supabase_client import ConflictError
from tests.test_assistant import initialized_assistant
from ui.gradio_app import Runtime, browser_value, create_app


def callbacks(assistant):
    demo = create_app(Runtime(assistant=assistant))
    return {fn.fn.__name__: fn.fn for fn in demo.fns.values() if fn.fn}


def test_restore_refreshes_version_preview_and_failed_consent_checkbox():
    assistant, credentials, snapshot = initialized_assistant()
    restore = callbacks(assistant)['restore']
    result = restore(browser_value(credentials))
    assert len(result) == 7
    assert result[1] == snapshot.version
    assert result[5] is True and result[6] is False
    assert restore(None)[6] is False


def test_stale_confirmation_preserves_chat_and_draft_and_requires_fresh_consent():
    assistant, credentials, snapshot = initialized_assistant()
    assistant.confirm = Mock(side_effect=ConflictError('Диалог изменился. Восстановите диалог.'))
    confirm = callbacks(assistant)['confirm']
    with patch('ui.gradio_app.gr.Warning') as warning:
        result = confirm(True, credentials, snapshot.version - 1)
    assert result[0] == gr.skip() and result[2] == gr.skip()
    assert result[3] == gr.skip() and result[4] is False
    assert 'Восстановите' in result[1]
    warning.assert_called_once()


def test_empty_send_preserves_input_and_never_calls_assistant():
    assistant, credentials, snapshot = initialized_assistant()
    assistant.send = Mock()
    send = callbacks(assistant)['send']
    with patch('ui.gradio_app.gr.Warning'):
        result = send('', credentials, snapshot.version)
    assistant.send.assert_not_called()
    assert result[0] == gr.skip() and result[4] == gr.skip()
    assert 'Введите вопрос' in result[1]
    assert result[5] is False
