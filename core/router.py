"""Локальная маршрутизация намерений. Не создаёт заявки и не подтверждает согласие."""

from dataclasses import dataclass
from enum import Enum
import re
from typing import Sequence

from llm.base import Message


class Intent(str, Enum):
    PRICE = "PRICE"
    BOOKING = "BOOKING"
    GENERAL = "GENERAL"
    OTHER = "OTHER"


class BookingStage(str, Enum):
    """В будущем передаётся только сервером из Memory, а не из текста клиента."""

    IDLE = "idle"
    TOPIC = "topic"
    NAME = "name"
    PHONE = "phone"
    CONSENT = "consent"


@dataclass(frozen=True)
class RouteResult:
    intent: Intent
    secondary_intents: tuple[Intent, ...] = ()
    legal_topic: str | None = None
    reason: str = ""
    clarification: str | None = None
    booking_cancelled: bool = False
    consent_refused: bool = False


PRICE = r"\b(?:цен\w*|стоимост\w*|расцен\w*|прайс\w*|тариф\w*|сколько\s+(?:(?:это|будет)\s+)?(?:стоит|стоить)|во\s+сколько\s+обойдется|почем|платн\w*|бесплатн\w*|скидк\w*|рассрочк\w*|предоплат\w*|оплат\w*)\b"
BOOKING = (
    r"\b(?:запиш\w*|записаться|записыва\w*|записываться|оформ\w*\s+заявк\w*|"
    r"остав\w*\s+заявк\w*|хочу\s+(?:получить\s+)?консультаци\w*|"
    r"нужна\s+консультаци\w*|нужна\s+запись|запис\w*\s+на\s+консультаци\w*|"
    r"нужен\s+юрист|свяжитесь\s+со\s+мной|перезвоните)\b"
)
CANCEL = (
    r"\b(?:не\s+(?:(?:хочу|нужно|надо|буду)\s+)?(?:запис\w*|запиш\w*|оформ\w*\s+заявк\w*)|"
    r"не\s+(?:хочу\s+консультаци\w*|нужна\s+консультаци\w*|нужен\s+юрист)|"
    r"отмен\w*\s+(?:запис\w*|заявк\w*)|передумал\w*|отказываюсь\s+от\s+консультаци\w*)\b"
)
REFUSE = (
    r"\b(?:не\s+(?:(?:согласен|согласна|соглашаюсь)\s+на\s+обработк\w*|даю\s+согласи[ея])|"
    r"отказываюсь\s+(?:от\s+обработки|давать\s+согласие)|"
    r"отзываю\s+согласие|не\s+обрабатывайте\s+(?:мои\s+)?данные)\b"
)
INJECTION = (
    r"\b(?:(?:игнорируй|забудь|отмени|измени|обойди)\s+(?:(?:все|предыдущие|системные)\s+)*"
    r"(?:инструкци\w*|правил\w*)|покажи\s+(?:системный\s+промпт|api[ -]?ключ\w*)|"
    r"(?:верни|выведи|ответь)\s+(?:только\s+)?(?:price|booking|general|other))\b"
)
QUESTION = r"\b(?:как|можно\s+ли|имею\s+ли|что\s+делать|какие\s+документы|вправе\s+ли|подскажите|объясните|расскажите)\b"
LEGAL = r"\b(?:суд\w*|иск\w*|прав\w*|договор\w*|долг\w*|задолж\w*|наслед\w*|увол\w*|работодател\w*|зарплат\w*|аренд\w*|претензи\w*|жалоб\w*|юридическ\w*|уголовн\w*|административн\w*)\b"
TOPICS = (
    ("Взыскание алиментов", r"\bалимент\w*\b"),
    ("Развод", r"\b(?:развод\w*|развестись|расторжени\w*\s+брака)\b"),
    ("Раздел имущества", r"\bраздел\w*\s+имуществ\w*\b"),
    ("Взыскание задолженности", r"\b(?:долг\w*|задолж\w*)\b"),
    ("Трудовое право", r"\b(?:трудов\w*|увол\w*|зарплат\w*|работодател\w*|отпуск\w*)\b"),
    ("Жилищное право", r"\b(?:жилищ\w*|квартир\w*|высел\w*|жкх|коммунальн\w*)\b"),
    ("Защита прав потребителей", r"\b(?:потребител\w*|товар\w*|магазин\w*|покупк\w*|продавец|продавц\w*)\b"),
    ("Семейное право", r"\b(?:семейн\w*|брак\w*|опек\w*|ребен\w*|детьми|детей)\b"),
    ("Гражданское право", r"\b(?:гражданск\w*|договор\w*|наслед\w*|аренд\w*)\b"),
)
PENDING = r"\b(?:как\s+(?:вас\s+)?зовут|ваше\s+имя|(?:укажите|назовите|сообщите|оставьте|напишите)\s+(?:(?:пожалуйста|ваше|ваш|кратко)\s+)*(?:имя|телефон|номер|тему|описание)|согласны\s+ли|даете\s+ли\s+согласие)\b"
SHORT_FOLLOWUP = r"^(?:да|нет|а\s+сколько|а\s+цена|подробнее|по\s+этому\s+вопросу|это\s+платно)[\s?!.]*$"
LEGAL_FOLLOWUP = (
    r"\b(?:что\s+еще\s+(?:нужно\s+)?(?:уточнить|сообщить|добавить)|"
    r"какие\s+(?:еще\s+)?обстоятельства|"
    r"(?:куплен\w*|приобретен\w*)\s+(?:вчера|сегодня|недавно)|"
    r"чек\s+(?:сохранился|есть|потерян)|"
    r"(?:документ\w*|переписк\w*|квитанци\w*)\s+(?:есть|сохранил\w*))\b"
)


def normalize(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None


def refuses_consent(text: str) -> bool:
    return (text.rstrip(" .!?") in {"не согласен", "не согласна", "не соглашаюсь"}
            or matches(REFUSE, text))


def explicit(text: str) -> tuple[Intent, ...]:
    """Цена имеет приоритет; запрос записи сохраняется как второе намерение."""
    result = []
    if matches(PRICE, text):
        result.append(Intent.PRICE)
    if matches(BOOKING, text) and not matches(CANCEL, text):
        result.append(Intent.BOOKING)
    if not result and (any(matches(pattern, text) for _, pattern in TOPICS)
                       or matches(LEGAL, text)):
        result.append(Intent.GENERAL)
    return tuple(result)


def topic_from(text: str) -> str | None:
    found = [topic for topic, pattern in TOPICS if matches(pattern, text)]
    # Несколько тем не сводим к одной услуге по произвольному приоритету.
    return "; ".join(found) if found else None


class Router:
    """Stateless: история только передаётся вызывающей стороной, внутри не хранится."""

    def route(self, message: str, history: Sequence[Message] = (), *,
              booking_stage: BookingStage = BookingStage.IDLE) -> RouteResult:
        if not isinstance(message, str) or len(message) > 4000:
            raise ValueError("Сообщение должно быть строкой длиной не более 4000 символов.")
        if not isinstance(booking_stage, BookingStage):
            raise ValueError("Этап записи должен быть значением BookingStage с сервера.")
        history = tuple(history)
        if len(history) > 100:
            raise ValueError("Передайте не более 100 последних сообщений истории.")
        for item in history:
            if (not isinstance(item, Message) or item.role not in {"user", "assistant"}
                    or not isinstance(item.content, str) or len(item.content) > 16000):
                raise ValueError("Некорректная история диалога.")
        text = normalize(message)
        if not text:
            return RouteResult(Intent.OTHER, reason="empty",
                               clarification="Напишите, пожалуйста, ваш вопрос.")
        if matches(INJECTION, text):
            return RouteResult(Intent.OTHER, reason="instruction_override",
                               clarification="Уточните юридический вопрос, стоимость услуги или желание записаться.")

        recent = history[-12:]
        cancelled = matches(CANCEL, text)
        refused = refuses_consent(text) or (
            booking_stage == BookingStage.CONSENT and text.rstrip(".! ") == "нет")
        direct = explicit(text)
        # Тему берём только из сообщений клиента, а не из предположений ассистента.
        topic = topic_from(text)
        if topic is None:
            for item in reversed(recent):
                prior = normalize(item.content)
                if item.role == "user" and not matches(INJECTION, prior):
                    topic = topic_from(prior)
                    if topic:
                        break

        def result(intent: Intent, reason: str, secondary: tuple[Intent, ...] = (),
                   clarification: str | None = None) -> RouteResult:
            return RouteResult(intent, secondary, topic, reason, clarification, cancelled, refused)

        active = booking_stage != BookingStage.IDLE
        # При отсутствии состояния — осторожное восстановление из недавней истории:
        # требуется намерение клиента записаться и последний уточняющий вопрос ассистента.
        if not active and recent and recent[-1].role == "assistant":
            requested = False
            for item in recent:
                if item.role == "user":
                    prior = normalize(item.content)
                    if matches(INJECTION, prior):
                        continue
                    if matches(CANCEL, prior) or refuses_consent(prior):
                        requested = False
                    elif Intent.BOOKING in explicit(prior):
                        requested = True
                    elif Intent.GENERAL in explicit(prior):
                        requested = False
            active = requested and matches(PENDING, normalize(recent[-1].content))
        if (active and recent and recent[-1].role == "assistant"
                and matches(r"согласи|согласны\s+ли", normalize(recent[-1].content))
                and text.rstrip(".! ") == "нет"):
            refused = True
        if cancelled or refused:
            # Флаги сигнализируют будущему Assistant остановить сбор; сами ничего не удаляют.
            if direct and direct[0] != Intent.BOOKING:
                return result(direct[0], "explicit_with_booking_stop", direct[1:])
            return result(Intent.BOOKING, "consent_refused" if refused else "booking_cancelled")
        if matches(r"^а\s+сколько[\s?!.]*$", text) and topic:
            return result(Intent.PRICE, "contextual_price")
        if direct:
            if (direct == (Intent.GENERAL,) and active
                    and not matches(QUESTION, text) and "?" not in text):
                return result(Intent.BOOKING, "booking_topic_or_details")
            return result(direct[0], "explicit", direct[1:])
        # Явный новый вопрос без юридической темы не считаем контактными данными.
        if active and not matches(QUESTION, text) and "?" not in text:
            return result(Intent.BOOKING, "booking_continuation")
        if matches(SHORT_FOLLOWUP, text):
            for item in reversed(recent):
                prior = normalize(item.content)
                if item.role != "user" or matches(INJECTION, prior):
                    continue
                if matches(CANCEL, prior) or refuses_consent(prior):
                    break
                previous = explicit(prior)
                if previous:
                    if previous[0] == Intent.BOOKING:
                        return result(Intent.OTHER, "ambiguous_booking_confirmation",
                                      clarification="Хотите оформить заявку на консультацию?")
                    return result(previous[0], "history_followup")
        # Факты и уточнения могут не повторять юридические ключевые слова.
        # Требуем прежний вопрос клиента; запись, отказ и новая явная тема
        # не должны автоматически продолжать консультацию.
        if not active and topic and matches(LEGAL_FOLLOWUP, text):
            for item in reversed(recent):
                prior = normalize(item.content)
                if item.role != "user" or matches(INJECTION, prior):
                    continue
                if matches(CANCEL, prior) or refuses_consent(prior):
                    break
                previous = explicit(prior)
                if previous:
                    if previous[0] == Intent.GENERAL:
                        return result(Intent.GENERAL, "legal_details_followup")
                    break
        return result(Intent.OTHER, "unrecognized",
                      clarification="Уточните, пожалуйста: вас интересует стоимость, запись или юридический вопрос?")
