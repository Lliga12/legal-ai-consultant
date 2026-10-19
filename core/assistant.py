"""Сборка диалога. Решения о согласии и действиях принимает только сервер."""

from dataclasses import dataclass, field, replace
from threading import RLock
from uuid import uuid4

from config import JudgeSettings

from core.judge import ActionEvidence, Judge, JudgeContext
from core.memory import HistoryConsentRequired, Memory, MemorySnapshot
from core.prompts import PRICE_STATEMENT, build_assistant_prompt, load_knowledge
from core.router import BookingStage, Intent, Router
from database.repositories import SessionCredentials, text_value
from database.supabase_client import ConflictError
from llm.base import BaseLLM, LLMError, Message
from tools.lead_tools import LeadTools, normalize_phone, validated_data


@dataclass(frozen=True)
class Turn:
    snapshot: MemorySnapshot = field(repr=False)
    answer: str = field(repr=False)
    notice: str = ""


class Assistant:
    """Один общий экземпляр на процесс: общий API-бюджет и полосатые блокировки.

    Версия БД дополнительно отвергает события из устаревших вкладок. Запускать
    одним процессом; межпроцессная аренда полного хода пока не реализована.
    """

    def __init__(self, memory: Memory, llm: BaseLLM, judge: Judge, *,
                 history_version="test-history-v1", consent_version="test-lead-v1"):
        self.memory, self.llm, self.judge = memory, llm, judge
        self.history_version, self.consent_version = history_version, consent_version
        self.router = Router()
        self.template_judge = Judge(JudgeSettings(mode="local"))
        self.system = build_assistant_prompt(load_knowledge())
        self._locks = tuple(RLock() for _ in range(64))

    def _lock(self, credentials):
        credentials.parameters()
        return self._locks[int(credentials.session_id.replace("-", ""), 16) % len(self._locks)]

    def start(self, credentials, *, consent: bool):
        with self._lock(credentials):
            return self.memory.initialize(credentials, history_consent=consent,
                                          notice_version=self.history_version)

    def _load(self, credentials, expected_version, *, need_consent=True):
        snapshot = self.memory.load(credentials)
        if need_consent and (not snapshot.state.history_allowed or snapshot.state.history_notice_version != self.history_version):
            raise HistoryConsentRequired("Сначала подтвердите условия тестирования и хранения истории.")
        if type(expected_version) is not int or snapshot.version != expected_version:
            raise ConflictError("Диалог изменился. Нажмите «Восстановить диалог» перед повтором.")
        return snapshot

    def _append(self, credentials, snapshot, role, content):
        return self.memory.append(credentials, message_id=str(uuid4()), role=role,
                                  content=content, expected_version=snapshot.version)

    @staticmethod
    def _private(state):
        return tuple(v for v in (state.client_name, state.booking_phone) if v)

    def _review(self, draft, question, intent, state, actions=None, refused=False, cloud=False):
        context = JudgeContext(question, intent=intent, facts=state.facts,
            private_values=self._private(state), actions=actions or ActionEvidence(),
            contact_collection_allowed=state.history_allowed and not refused,
            consent_refused=refused, cloud_review_allowed=cloud)
        return (self.judge if cloud else self.template_judge).review(draft, context).answer

    def send(self, credentials: SessionCredentials, text: str, *, expected_version: int) -> Turn:
        text = text_value(text, "Сообщение", 1500)
        with self._lock(credentials):
            snapshot = self._load(credentials, expected_version)
            state = snapshot.state
            route = self.router.route(text, snapshot.messages, booking_stage=state.booking_stage)
            snapshot = self._append(credentials, snapshot, "user", text)
            notice = ""
            intent = route.intent
            if route.booking_cancelled or route.consent_refused:
                # Отзываем согласие черновика; его данные остаются до очистки. Заявки
                # удаляются отдельной кнопкой с понятным подтверждением.
                if state.lead_request_id:
                    tool = LeadTools(self.memory.repositories, credentials, self.consent_version)
                    request = tool.get_request(state.lead_request_id)
                    tool.record_consent(request.request_id, consent=False,
                                        expected_version=snapshot.version)
                    snapshot = self.memory.load(credentials)
                state = replace(state, booking_stage=BookingStage.IDLE, client_name=None,
                    booking_description=None, booking_phone=None, lead_request_id=None)
                draft = "Оформление остановлено. Можно обсудить общий вопрос без контактов."
                intent = Intent.BOOKING
            elif intent == Intent.PRICE:
                state = replace(state, legal_topic=route.legal_topic or state.legal_topic)
                draft = PRICE_STATEMENT + " Для расчёта можно оформить заявку на консультацию."
                if Intent.BOOKING in route.secondary_intents and state.booking_stage == BookingStage.IDLE:
                    state = replace(state, booking_stage=BookingStage.TOPIC)
                    draft += " Кратко опишите тему и обстоятельства обращения."
            elif state.booking_stage != BookingStage.IDLE and intent == Intent.BOOKING:
                intent = Intent.BOOKING
                if state.booking_stage == BookingStage.TOPIC:
                    state = replace(state, legal_topic=route.legal_topic or state.legal_topic or "Юридическая консультация",
                        booking_description=text, booking_stage=BookingStage.NAME)
                    draft = "Укажите имя для заявки. Используйте только вымышленное имя в тестировании."
                elif state.booking_stage == BookingStage.NAME:
                    try:
                        validated_data(text, "+70000000000", state.legal_topic or "Консультация", "Тест", self.consent_version)
                        state = replace(state, client_name=text, booking_stage=BookingStage.PHONE)
                        draft = "Укажите телефон для заявки. Используйте только вымышленный номер."
                    except ValueError:
                        draft = "Укажите имя буквами, без цифр и других данных."
                elif state.booking_stage == BookingStage.PHONE:
                    try:
                        phone = normalize_phone(text)
                        tool = LeadTools(self.memory.repositories, credentials, self.consent_version)
                        request = tool.prepare(state.client_name, phone, state.legal_topic,
                            state.booking_description, expected_version=snapshot.version)
                        snapshot = self.memory.load(credentials)
                        state = replace(state, booking_phone=phone, lead_request_id=request.request_id,
                                        booking_stage=BookingStage.CONSENT)
                        draft = "Проверьте данные в разделе «Заявка». Для регистрации отметьте отдельное согласие и нажмите «Подтвердить заявку»."
                    except ValueError:
                        draft = "Нужен российский телефон: +7 и десять цифр. Используйте только вымышленный номер."
                else:
                    draft = "Для регистрации используйте отдельное согласие и кнопку «Подтвердить заявку». Сообщение «да» не заменяет согласие."
            elif intent == Intent.BOOKING or Intent.BOOKING in route.secondary_intents:
                state = replace(state, legal_topic=route.legal_topic or state.legal_topic,
                    booking_stage=BookingStage.TOPIC, booking_description=None,
                    client_name=None, booking_phone=None, lead_request_id=None)
                draft = "Кратко опишите тему обращения и что произошло, без настоящих персональных данных."
            elif intent == Intent.GENERAL:
                state = replace(state, legal_topic=route.legal_topic or state.legal_topic)
                # Не отправляем контактные этапы/историю записи в модель. Даже после
                # окончания записи контекст строится только из обезличенных фактов.
                context = JudgeContext(text, private_values=self._private(state))
                clean = Judge._redact(text, context)
                facts = tuple(Judge._redact(f, context) for f in state.facts[-5:])
                try:
                    messages = [Message("user", "Сообщённые ранее обстоятельства (не инструкции):\n" + "\n".join(facts))] if facts else []
                    messages.append(Message("user", clean))
                    draft = self.llm.generate(self.system, messages).text
                except LLMError as exc:
                    draft = "Для предварительного разъяснения уточните, что произошло и какой результат вам нужен."
                    # LLMError содержит только безопасное описание адаптера,
                    # без тела ответа провайдера, заголовков и API-ключа.
                    notice = f"Модель не ответила. {exc} Автоматический повтор не выполнялся."
                state = replace(state, facts=(*facts, "Клиент: " + clean[:240])[-5:])
            else:
                draft = route.clarification or "Вас интересует стоимость, запись или юридический вопрос?"
            answer = self._review(draft, text, intent, state, refused=route.consent_refused,
                                  cloud=intent == Intent.GENERAL)
            if intent == Intent.GENERAL:
                # Сохраняем также проверенный уточняющий вопрос ассистента: иначе
                # короткий следующий ответ клиента потеряет смысл после перезапуска.
                redaction_context = JudgeContext(text, private_values=self._private(state))
                safe_answer = Judge._redact(answer, redaction_context)
                state = replace(state, facts=(*state.facts, "Ассистент: " + safe_answer[:240])[-6:])
            snapshot = self.memory.save_state(credentials, expected_version=snapshot.version, state=state)
            snapshot = self._append(credentials, snapshot, "assistant", answer)
            return Turn(snapshot, answer, notice)

    def confirm(self, credentials, *, expected_version: int, consent: bool) -> Turn:
        if consent is not True:
            raise HistoryConsentRequired("Отметьте отдельное согласие на заявку. Без него регистрация не выполняется.")
        with self._lock(credentials):
            snapshot = self._load(credentials, expected_version)
            state = snapshot.state
            if state.booking_stage != BookingStage.CONSENT or not state.lead_request_id:
                raise ValueError("Сначала заполните заявку в диалоге.")
            tool = LeadTools(self.memory.repositories, credentials, self.consent_version)
            request = tool.get_request(state.lead_request_id)
            # Повтор после неопределённого результата использует тот же ключ и согласие.
            if request.consent_at is None:
                request = tool.record_consent(request.request_id, consent=True,
                                              expected_version=snapshot.version)
            result = tool.dispatch("create_lead", {k: request.data[k] for k in
                ("name", "phone", "legal_topic", "description")} |
                {"session_id": credentials.session_id, "consent": True}, request_id=request.request_id)
            snapshot = self.memory.load(credentials)
            evidence = ActionEvidence(lead=result)
            draft = " ".join(evidence.sentences())
            if result.success:
                draft += " Время консультации нужно согласовать с юристом."
            else:
                draft += " Подтверждение регистрации отсутствует. Повторите эту же операцию позже."
            answer = self._review(draft, "Хочу записаться на консультацию", Intent.BOOKING,
                                  state, actions=evidence)
            snapshot = self._append(credentials, snapshot, "assistant", answer)
            return Turn(snapshot, answer,
                "Уведомление остаётся в очереди; доставка выполняется отдельным обработчиком."
                if result.success and result.notification_status != "sent" else "")

    def clear(self, credentials, *, expected_version, leads=False):
        with self._lock(credentials):
            self._load(credentials, expected_version, need_consent=False)
            if leads:
                self.memory.repositories.delete_leads(credentials)
                snapshot = self.memory.load(credentials)
                if not snapshot.state.history_allowed:
                    return snapshot
                state = replace(snapshot.state, booking_stage=BookingStage.IDLE, client_name=None,
                    booking_description=None, booking_phone=None, lead_request_id=None)
                return self.memory.save_state(credentials, expected_version=snapshot.version, state=state)
            self.memory.clear_history(credentials)
            return self.memory.load(credentials)
