"""Защита тестового размещения: без сети и действующих ключей."""

from unittest.mock import patch

import pytest

from config import ConfigError, WebSettings, load_web_settings


def settings(env):
    with patch.dict("os.environ", env, clear=True), patch("config.load_dotenv"):
        return load_web_settings()


def test_local_start_without_auth_is_preserved():
    assert settings({}) == WebSettings()


@pytest.mark.parametrize("env", [
    {"RENDER": "true"},
    {"RENDER": "true", "WEB_AUTH_USERNAME": "tester"},
    {"RENDER": "true", "WEB_AUTH_PASSWORD": "a" * 20},
    {"RENDER": "true", "WEB_AUTH_USERNAME": "tester", "WEB_AUTH_PASSWORD": "short"},
    {"WEB_AUTH_USERNAME": "tester"},
    {"WEB_AUTH_PASSWORD": "a" * 20},
])
def test_incomplete_credentials_fail_closed_without_printing_password(env):
    with pytest.raises(ConfigError) as error:
        settings(env)
    assert env.get("WEB_AUTH_PASSWORD", "not-present") not in str(error.value)


def test_render_uses_port_and_exact_password_without_exposing_secrets():
    configured = settings({"RENDER": "true", "PORT": "10000",
        "WEB_AUTH_USERNAME": "Тест", "WEB_AUTH_PASSWORD": "Проверка-пароля-123456"})
    assert configured.port == 10000
    assert configured.authenticate("Тест", "Проверка-пароля-123456")
    assert not configured.authenticate("Другой", "Проверка-пароля-123456")
    assert not configured.authenticate("Тест", "wrong")
    assert not configured.authenticate(None, None)
    assert "Проверка" not in repr(configured)


@pytest.mark.parametrize("port", ["0", "65536", "text"])
def test_bad_render_port_is_rejected(port):
    with pytest.raises(ConfigError):
        settings({"PORT": port})
