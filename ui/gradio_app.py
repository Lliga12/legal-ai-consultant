"""Русскоязычный Gradio. На этапе 10 разрешено только тестирование."""

import atexit
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
from dotenv import load_dotenv

from config import (ROOT, ConfigError, load_judge_settings, load_settings,
                    load_supabase_settings, load_web_settings)
from core.assistant import Assistant
from core.diagnostics import report_input_error
from core.judge import Judge
from core.memory import HistoryConsentRequired, Memory
from core.prompts import DISCLAIMER
from database.repositories import Repositories, SessionCredentials
from database.supabase_client import ConflictError, DatabaseError, SupabaseClient
from llm.factory import create_llm


WELCOME = "Здравствуйте! Я помогу уточнить юридический вопрос, стоимость и оформить заявку на консультацию."
# Встроенный clear Gradio меняет только виджет, не БД. Оставляем явную кнопку
# удаления истории с подтверждением; прочие встроенные toolbar-кнопки выключены.
UI_CSS = """
#legal-chat button.icon-button { display: none !important; }
#voice-welcome {
    background: linear-gradient(115deg, #fff7ed, #fffdf9) !important;
    border: 2px solid #fb923c !important; border-radius: 20px !important;
    padding: 24px !important; gap: 16px !important;
    box-shadow: 0 8px 28px rgba(194, 91, 12, .10); margin: 16px 0 !important;
}
.voice-heading { display: flex; align-items: center; gap: 22px; color: #292018; }
.voice-symbol {
    display: grid; place-items: center; flex: 0 0 88px; height: 88px;
    border-radius: 50%; color: #fff; background: linear-gradient(140deg,#ffad4d,#ed650a);
    box-shadow: 0 6px 18px rgba(234, 88, 12, .20);
}
.voice-heading h2 { margin: 0 0 8px !important; font-size: clamp(24px,3vw,32px) !important;
    line-height: 1.2 !important; font-weight: 750 !important; color: #292018 !important; }
.voice-heading p { margin: 0 !important; font-size: 17px !important; color: #675347 !important; }
#voice-start {
    background: #e85b04 !important; color: #fff !important; border: 0 !important;
    min-height: 62px !important; font-size: 20px !important; font-weight: 700 !important;
    border-radius: 12px !important; box-shadow: 0 4px 12px rgba(194, 65, 12, .18);
}
#voice-start:hover:not(:disabled) { background: #c84d03 !important; }
#voice-start:focus-visible { outline: 3px solid #9a3412 !important; outline-offset: 4px; }
#voice-start:disabled { opacity: .55; }
@media (max-width: 600px) {
    #voice-welcome { padding: 18px !important; }
    .voice-heading { align-items: flex-start; gap: 14px; }
    .voice-symbol { flex-basis: 60px; height: 60px; }
    .voice-symbol svg { width: 30px; height: 30px; }
}
"""
VOICE_HEADER = """
<div class="voice-heading">
  <div class="voice-symbol" aria-hidden="true">
    <svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <rect x="9" y="2" width="6" height="12" rx="3"></rect>
      <path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"></path>
    </svg>
  </div>
  <div><h2>Поговорите с помощником юриста</h2>
    <p>Расскажите о своей ситуации — помощник ответит голосом.</p>
  </div>
</div>
"""
TEST_NOTICE = (
    "**Тестовая версия. Используйте только вымышленные данные.** Например, имя Тест и телефон +70000000000. "
    "Реальные персональные данные пока не принимаются. После согласия переписка и этап записи сохраняются "
    "в настроенной Supabase; обезличенный текст общего вопроса передаётся выбранному AI-провайдеру. "
    "Автоматическое удаление всех косвенных идентификаторов не гарантируется. "
    "Доступ к диалогу сохраняется в этом браузере: не используйте общее устройство. "
    "Очистка истории сбрасывает согласие и черновик; сохранённые заявки удаляются отдельно."
)
LEAD_NOTICE = (
    "Согласие на тестовую заявку: имя, телефон, тема и описание будут сохранены в Supabase "
    "для обработки обращения. В Telegram юристу через отдельный обработчик передаются ID, дата, "
    "первое слово имени, общая тема и маскированный контакт (либо полный при серверной настройке). "
    "Это отдельное действие от согласия на переписку. Уже доставленное сообщение Telegram "
    "кнопка удаления заявки не удаляет. Полноценные тексты политики и согласий с реквизитами "
    "оператора должны быть подготовлены до публичного запуска."
)


@dataclass
class Runtime:
    assistant: Assistant | None = None
    issue: str = ""
    # BrowserState передаёт ключ шифрования фронтенду: это не серверный секрет
    # и не авторизация. Право доступа даёт отдельный 256-битный token каждой сессии.
    browser_secret: str = field(default="legal-ai-test-browser-storage-v1", repr=False)
    policy_url: str | None = None


def load_runtime() -> Runtime:
    load_dotenv(ROOT / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    runtime = Runtime()
    policy = os.getenv("PRIVACY_POLICY_URL", "").strip()
    if policy:
        url = urlsplit(policy)
        if url.scheme != "https" or not url.netloc or url.username or url.password or any(c in policy for c in '\"<> []'):
            runtime.issue = "PRIVACY_POLICY_URL: нужна корректная ссылка HTTPS."
            return runtime
        runtime.policy_url = policy
    client = llm = None
    try:
        settings, db_settings, judge_settings = load_settings(), load_supabase_settings(), load_judge_settings()
        client = SupabaseClient(db_settings)
        repo = Repositories(client)
        repo.health()
        repo.memory_health()
        repo.tools_health()
        repo.notifications_health()
        llm = create_llm(settings)
        runtime.assistant = Assistant(Memory(repo), llm, Judge(judge_settings, llm))
        atexit.register(client.close)
        atexit.register(llm.close)
    except ConfigError:
        runtime.issue = "Проверьте .env: настройки модели, Supabase и Judge не заполнены или некорректны."
    except DatabaseError:
        runtime.issue = "Supabase недоступен или не применены schema.sql и миграции 002–004. Диалог не включён."
    finally:
        if runtime.assistant is None:
            if client:
                client.close()
            if llm:
                llm.close()
    return runtime


def credentials_from_browser(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"session_id", "token"}:
        raise ValueError("Доступ к сохранённой сессии некорректен.")
    credentials = SessionCredentials(value["session_id"], value["token"])
    credentials.parameters()
    return credentials


def browser_value(credentials):
    return {"session_id": credentials.session_id, "token": credentials.token} if credentials else None


def chat_value(snapshot):
    return [{"role": "assistant", "content": WELCOME}] + [
        {"role": message.role, "content": message.content} for message in snapshot.messages]


def booking_value(snapshot):
    state = snapshot.state
    stages = {"idle": "запись не начата", "topic": "тема и описание", "name": "имя",
              "phone": "телефон", "consent": "проверка данных и согласие"}
    return "\n".join(("Этап: " + stages[state.booking_stage.value],
        "Тема: " + (state.legal_topic or "—"), "Описание: " + (state.booking_description or "—"),
        "Имя: " + (state.client_name or "—"), "Телефон: " + (state.booking_phone or "—")))


def create_app(runtime: Runtime | None = None, voice_service=None):
    runtime = runtime or load_runtime()
    assistant = runtime.assistant

    def ready():
        if assistant is None:
            raise gr.Error(runtime.issue or "Сервер ещё не настроен.")

    def safe(action):
        try:
            ready()
            return action()
        except (HistoryConsentRequired, ConflictError) as exc:
            raise gr.Error(str(exc)) from None
        except DatabaseError:
            raise gr.Error("Не удалось подтвердить операцию в БД. Восстановите диалог; повтор заявки использует тот же черновик.") from None
        except (ValueError, TypeError, AttributeError) as exc:
            code = report_input_error(exc)
            raise gr.Error("Не удалось завершить операцию. "
                           f"Код диагностики: {code}. "
                           "Восстановите диалог перед повтором.") from None

    def start(consent, saved):
        def operation():
            if consent is not True:
                raise HistoryConsentRequired("Перед началом подтвердите условия тестирования и хранения истории.")
            credentials = credentials_from_browser(saved) or assistant.memory.repositories.new_credentials()
            snapshot = assistant.start(credentials, consent=True)
            return credentials, snapshot.version, browser_value(credentials), chat_value(snapshot), "Диалог готов.", booking_value(snapshot)
        return safe(operation)

    def restore(saved):
        if saved is None or assistant is None:
            return None, None, [{"role": "assistant", "content": WELCOME}], runtime.issue or "Подтвердите условия и начните диалог.", "", False
        def operation():
            credentials = credentials_from_browser(saved)
            snapshot = assistant.memory.load(credentials)
            return credentials, snapshot.version, chat_value(snapshot), "Диалог восстановлен из БД.", booking_value(snapshot), snapshot.state.history_allowed
        return safe(operation)

    def send(text, credentials, version):
        def operation():
            if credentials is None:
                raise HistoryConsentRequired("Сначала подтвердите условия и начните диалог.")
            turn = assistant.send(credentials, text, expected_version=version)
            return chat_value(turn.snapshot), turn.notice or "Ответ подготовлен. Информация предварительная.", booking_value(turn.snapshot), turn.snapshot.version, "", False
        return safe(operation)

    def confirm(consent, credentials, version):
        def operation():
            if credentials is None:
                raise HistoryConsentRequired("Сначала начните диалог.")
            turn = assistant.confirm(credentials, expected_version=version, consent=consent)
            return chat_value(turn.snapshot), turn.notice or "Операция обработана.", booking_value(turn.snapshot), turn.snapshot.version, False
        return safe(operation)

    def clear(confirmed, credentials, version, leads=False):
        def operation():
            if confirmed is not True:
                raise HistoryConsentRequired("Подтвердите удаление отдельным флажком.")
            if credentials is None:
                raise HistoryConsentRequired("Сначала начните или восстановите диалог.")
            snapshot = assistant.clear(credentials, expected_version=version, leads=leads)
            return chat_value(snapshot), ("Заявки и задачи уведомлений удалены. Переписка сохранена." if leads else
                "История и черновик очищены. Перед новым сообщением снова подтвердите условия."), booking_value(snapshot), snapshot.version, snapshot.state.history_allowed, False, False
        return safe(operation)

    with gr.Blocks(title="AI-консультант юриста", analytics_enabled=False) as demo:
        gr.Markdown("# AI-консультант юриста\nСмоленск · консультации для физических лиц по всей России")
        gr.Markdown(DISCLAIMER)
        if assistant is not None and runtime.issue:
            gr.Markdown(runtime.issue)
        gr.Markdown(TEST_NOTICE, elem_id="test-data")
        if runtime.policy_url:
            gr.Markdown(f"[Политика обработки персональных данных]({runtime.policy_url})")
        else:
            gr.Markdown("Политика обработки персональных данных: ссылка оператора пока не задана. Реальные данные вводить нельзя.")
        gr.Markdown("[Условия обработки тестовых данных](#test-data)")
        credentials = gr.State(None)
        version = gr.State(None)
        saved = gr.BrowserState(None, storage_key="legal_ai_test_session_v1", secret=runtime.browser_secret)
        status = gr.Textbox(value=runtime.issue or "Подтвердите условия и начните диалог.", label="Состояние", interactive=False)
        history_consent = gr.Checkbox(False, label="Использую вымышленные данные и согласен на сохранение переписки и передачу общего вопроса AI-провайдеру")
        with gr.Row():
            begin = gr.Button("Начать диалог", variant="primary", interactive=assistant is not None)
            reload_button = gr.Button("Восстановить диалог", interactive=assistant is not None)
        if voice_service is not None:
            with gr.Column(elem_id="voice-welcome"):
                gr.HTML(VOICE_HEADER)
                gr.Markdown("Голос передаётся Яндексу для распознавания и ответа. Только вымышленные данные. "
                    "Аудио и полная расшифровка не сохраняются нашим сайтом; Яндекс обрабатывает их по своим условиям. "
                    "Черновик заявки сохраняется в вашей Supabase после просьбы оформить его. "
                    "Голосовые ответы экспериментальные и не проходят проверку текстового чата. "
                    "До 3 минут, один разговор одновременно. Сначала начните текстовый диалог кнопкой выше.")
                voice_consent = gr.Checkbox(False, label="Согласен на передачу моего тестового голоса Яндексу и подготовку черновика заявки")
                voice_begin = gr.Button("Начать разговор", variant="primary", size="lg",
                    elem_id="voice-start", interactive=assistant is not None)
                voice_frame = gr.HTML()
                gr.Markdown("После подготовки голосовой заявки завершите разговор и нажмите «Восстановить диалог» выше. "
                    "Затем проверьте заявку и подтвердите её отдельной кнопкой ниже. Время согласует юрист.")

                def open_voice(consent, c, v):
                    try:
                        token = voice_service.issue(consent, c, v)
                    except ValueError as exc:
                        raise gr.Error(str(exc)) from None
                    return ('<iframe title="Голосовой помощник" src="/voice/#' + token +
                            '" allow="microphone; autoplay" style="width:100%;height:430px;border:0;border-radius:12px"></iframe>')

                voice_begin.click(open_voice, [voice_consent, credentials, version], [voice_frame],
                    api_visibility="private", concurrency_id="dialog", concurrency_limit=1)
        new_button = gr.Button("Новый диалог — забыть доступ к прежнему")
        gr.Markdown("Новый диалог убирает доступ из браузера, но не удаляет данные в БД. Сначала удалите ненужные заявки и историю кнопками ниже.")
        chat = gr.Chatbot(value=[{"role": "assistant", "content": WELCOME}], label="Переписка",
                          height=400, sanitize_html=True, render_markdown=False, buttons=[],
                          feedback_options=(), allow_file_downloads=False, elem_id="legal-chat")
        text = gr.Textbox(label="Ваш вопрос", placeholder="Например: как взыскать алименты?", max_length=1500, lines=2, interactive=assistant is not None)
        send_button = gr.Button("Отправить", variant="primary", interactive=assistant is not None)
        with gr.Accordion("Заявка", open=True):
            preview = gr.Textbox(label="Данные заявки — проверьте перед подтверждением", interactive=False, lines=6)
            gr.Markdown(LEAD_NOTICE)
            lead_consent = gr.Checkbox(False, label="Согласен на обработку указанных тестовых данных для заявки")
            confirm_button = gr.Button("Подтвердить заявку", interactive=assistant is not None)
        with gr.Accordion("Управление данными", open=False):
            gr.Markdown("Очистка истории сохраняет зарегистрированные заявки. Удаление заявок сохраняет переписку. Доставленные уведомления Telegram остаются у получателя.")
            delete_confirmation = gr.Checkbox(False, label="Подтверждаю выбранное удаление")
            with gr.Row():
                clear_button = gr.Button("Очистить историю", interactive=assistant is not None)
                delete_button = gr.Button("Удалить мои заявки", interactive=assistant is not None)
        # Все операции одной группы сериализованы; API скрыт, но каждый вызов
        # всё равно проверяет владение сессией и версию в БД.
        event = dict(api_visibility="private", concurrency_id="dialog", concurrency_limit=1, show_progress="full")
        begin.click(start, [history_consent, saved], [credentials, version, saved, chat, status, preview], **event)
        restore_outputs = [credentials, version, chat, status, preview, history_consent]
        reload_button.click(restore, [saved], restore_outputs, **event)
        new_button.click(lambda: (None, None, None, [{"role": "assistant", "content": WELCOME}],
            "Доступ к прежнему диалогу убран из этого браузера. Подтвердите условия для нового.", "", False, False),
            [], [credentials, version, saved, chat, status, preview, history_consent, lead_consent], **event)
        demo.load(restore, [saved], restore_outputs, **event)
        outputs = [chat, status, preview, version, text, lead_consent]
        send_button.click(send, [text, credentials, version], outputs, **event)
        text.submit(send, [text, credentials, version], outputs, **event)
        confirm_button.click(confirm, [lead_consent, credentials, version], [chat, status, preview, version, lead_consent], **event)
        deletion_outputs = [chat, status, preview, version, history_consent, lead_consent, delete_confirmation]
        clear_button.click(clear, [delete_confirmation, credentials, version], deletion_outputs, **event)
        delete_button.click(lambda ok, c, v: clear(ok, c, v, leads=True), [delete_confirmation, credentials, version], deletion_outputs, **event)
    demo.queue(max_size=32, default_concurrency_limit=1)
    return demo


def create_server(runtime=None, settings=None):
    from fastapi import FastAPI
    from ui.voice import VoiceService, install_routes
    settings = settings or load_web_settings()
    runtime = runtime or load_runtime()
    voice = VoiceService(runtime.assistant)
    demo = create_app(runtime, voice_service=voice)
    demo.state_session_capacity = 1000
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # Install before the root mount. Reuse Gradio's login cookies for HTTP and WS.
    install_routes(app, voice, lambda: app.routes[-1].app, settings.username is not None)
    return gr.mount_gradio_app(app, demo, path="/",
        auth=settings.authenticate if settings.username is not None else None,
        auth_message="Тестовый доступ к AI-консультанту. Используйте только вымышленные данные.",
        show_error=False, footer_links=[], run_history=False,
        css=UI_CSS,
        blocked_paths=[str(ROOT / ".env"), str(ROOT / ".git"), str(ROOT / "database")])


def launch():
    import uvicorn
    settings = load_web_settings()
    uvicorn.run(create_server(settings=settings), host="0.0.0.0", port=settings.port,
                ws_max_size=65536, ws="websockets", log_level="warning")
