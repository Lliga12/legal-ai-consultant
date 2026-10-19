"""HTTP-проверка входа Gradio без LLM, Supabase и Telegram."""

from pathlib import Path
import secrets
import socket
import sys
from unittest.mock import patch
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from config import load_web_settings
    from ui.gradio_app import Runtime, UI_CSS, create_app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    password = secrets.token_urlsafe(32)
    with patch.dict("os.environ", {"RENDER": "true", "PORT": str(port),
        "WEB_AUTH_USERNAME": "test-user", "WEB_AUTH_PASSWORD": password}), patch("config.load_dotenv"):
        settings = load_web_settings()
    demo = create_app(Runtime(issue="Local deployment check: integrations disabled."))
    try:
        demo.launch(server_name="127.0.0.1", server_port=settings.port,
            auth=settings.authenticate, share=False, inbrowser=False,
            prevent_thread_lock=True, quiet=True, footer_links=[], css=UI_CSS,
            blocked_paths=[str(ROOT / ".env"), str(ROOT / ".git"), str(ROOT / "database")])
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15) as client:
            assert client.get("/").status_code == 200
            assert client.get("/config").status_code == 401
            assert client.post("/login", data={"username": "test-user", "password": "wrong"}).status_code == 400
            assert client.post("/login", data={"username": "test-user", "password": password}).status_code == 200
            config = client.get("/config")
            assert config.status_code == 200
            assert password not in config.text
            path = quote(str(ROOT / "database/schema.sql"), safe="")
            assert client.get("/gradio_api/file=" + path).status_code in {403, 404}
        print("PASS: Gradio HTTP login, anonymous API rejection, password not exposed, SQL files blocked")
    finally:
        demo.close()


if __name__ == "__main__":
    main()
