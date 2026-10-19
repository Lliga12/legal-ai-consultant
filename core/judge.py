"""Консервативная проверка перед отправкой. Не является юридической экспертизой."""

from dataclasses import dataclass, field
from enum import Enum
import json
import re
import unicodedata

from config import JudgeSettings, load_judge_settings
from core.prompts import DISCLAIMER, JUDGE_RULES, PRICE_STATEMENT, load_knowledge
from core.router import Intent, normalize
from database.repositories import uuid_text
from llm.base import BaseLLM, LLMError, Message
from tools.lead_tools import LeadResult, LeadStatus


class Issue(str,Enum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    IRRELEVANT = "irrelevant"
    PRICE = "unsupported_price"
    GUARANTEE = "guarantee"
    LAW = "unverified_law"
    FACT = "unsupported_fact"
    ACTION = "unsupported_action"
    PERSONAL = "personal_data"
    SECRET = "secret"
    CONSENT = "consent"
    INJECTION = "instruction_override"
    SEMANTIC_REJECTED = "semantic_rejected"
    SEMANTIC_UNAVAILABLE = "semantic_unavailable"
    SEMANTIC_INVALID = "semantic_invalid"


@dataclass(frozen=True)
class ActionEvidence:
    """Только серверные результаты текущего обращения, не JSON из ответа модели."""
    lead: LeadResult | None = field(default=None,repr=False)
    notification_sent: bool = False
    history_deleted_count: int | None = None
    leads_deleted_count: int | None = None
    session_deleted: bool = False

    def __post_init__(self) -> None:
        if self.lead is not None and not isinstance(self.lead,LeadResult):
            raise ValueError("Требуется серверный LeadResult.")
        if self.lead is not None and self.lead.success:
            uuid_text(self.lead.lead_id)
            if not self.lead.created_at:
                raise ValueError("Нет даты подтверждённой заявки.")
        for value in (self.notification_sent,self.session_deleted):
            if type(value) is not bool:
                raise ValueError("Подтверждение действия должно быть boolean.")
        for value in (self.history_deleted_count,self.leads_deleted_count):
            if value is not None and (type(value) is not int or value<0):
                raise ValueError("Требуется число удалённых записей.")

    def sentences(self) -> tuple[str,...]:
        result = []
        if self.lead is not None:
            if self.lead.success:
                result.append("Заявка зарегистрирована ранее." if self.lead.status==LeadStatus.ALREADY_EXISTS
                              else "Заявка зарегистрирована.")
            elif self.lead.status==LeadStatus.UNCONFIRMED:
                result.append("Регистрация заявки не подтверждена.")
        if self.notification_sent or (self.lead is not None and self.lead.success
                                      and self.lead.notification_status=="sent"):
            result.append("Уведомление отправлено юристу.")
        if self.history_deleted_count is not None:
            result.append("История переписки очищена.")
        if self.leads_deleted_count is not None:
            result.append("Заявки удалены.")
        if self.session_deleted:
            result.append("Сессия удалена.")
        return tuple(result)


@dataclass(frozen=True)
class JudgeContext:
    question: str = field(repr=False)
    intent: Intent = Intent.GENERAL
    facts: tuple[str,...] = field(default=(),repr=False)
    private_values: tuple[str,...] = field(default=(),repr=False)
    secrets: tuple[str,...] = field(default=(),repr=False)
    actions: ActionEvidence = field(default_factory=ActionEvidence)
    contact_collection_allowed: bool = False
    consent_refused: bool = False
    cloud_review_allowed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.question,str) or not self.question.strip() or len(self.question)>4000:
            raise ValueError("Вопрос Judge: от 1 до 4000 символов.")
        if not isinstance(self.intent,Intent) or not isinstance(self.actions,ActionEvidence):
            raise ValueError("Нужны серверное намерение и подтверждения действий.")
        for values in (self.facts,self.private_values,self.secrets):
            if not isinstance(values,tuple) or len(values)>40 or any(
                not isinstance(v,str) or not v.strip() or len(v)>1000 for v in values):
                raise ValueError("Контекст Judge: кортеж до 40 непустых строк до 1000 символов.")
        if any(type(v) is not bool for v in (self.contact_collection_allowed,self.consent_refused,
                                           self.cloud_review_allowed)):
            raise ValueError("Разрешения Judge задаются сервером как boolean.")


@dataclass(frozen=True)
class JudgeResult:
    answer: str = field(repr=False)
    issues: tuple[Issue,...] = ()
    replaced: bool = False
    semantic_checked: bool = False

    @property
    def approved(self) -> bool:
        """Черновик прошёл заданные проверки; не сертификат юридической достоверности."""
        return not self.replaced


def clean_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC",text)
    return "".join(c for c in text if unicodedata.category(c)!="Cf")


def canonical(text: str) -> str:
    return normalize(clean_unicode(text))


PHONE = r"(?<![\w])(?:\+?7|8)[ ()\-.]*[0-9](?:[ ()\-.]*[0-9]){9}(?![0-9])"
EMAIL = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
IDENTIFIER = r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
SENSITIVE = r"\b(?:[0-9]{4}\s+[0-9]{6}|[0-9]{3}[- ][0-9]{3}[- ][0-9]{3}[- ]?[0-9]{2}|[0-9](?:[ -]*[0-9]){12,18})\b"
SECRET = r"(?:sb_secret_[A-Za-z0-9_-]+|\bsk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{15,}|\bbot?[0-9]{5,20}:[A-Za-z0-9_-]{20,}|\b[0-9]{5,20}:[A-Za-z0-9_-]{20,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|(?:api[_ -]?key|токен|пароль)\s*[:=]\s*\S{4,})"
MONEY = r"(?:[0-9][0-9\s.,]*\s*(?:₽|руб\w*|р\.|доллар\w*|евро|\$)|[₽$]\s*[0-9]|(?:пять|десять|две|три|тысяч\w*|сот\w*)\s+(?:тысяч\w*\s+)?(?:руб\w*|доллар\w*))"
GUARANTEE = r"\b(?:гарантир\w*|гарантирован\w*|100\s*%|точно\s+(?:выигра\w*|взыщ\w*|получ\w*)|обязательно\s+выигра\w*|суд\s+(?:точно|обязательно)\s+\w+)"
LAW = r"(?:\b(?:стать[яеию]|ст\.)\s*[0-9]|\b(?:фз|федеральн\w*\s+закон\w*)\s*(?:№|n)?\s*[0-9]|[0-9]+\s*[-–]\s*фз\b|\b(?:по закону|срок\w*|обязан\w*|должны|не позднее|в течение)\b.{0,65}\b[0-9]+\s*(?:дн\w*|день|дней|сут\w*|час\w*|месяц\w*|лет|год\w*)|\b(?:тк|гк|ск|жк|гпк|ук)\s+рф\b)"
ACTION = r"\b(?:(?:заявк\w*|запис\w*).{0,30}(?:создан\w*|зарегистрирован\w*|сохранен\w*|оформлен\w*|удален\w*|подтвержден\w*)|(?:создал\w*|зарегистрировал\w*|сохранил\w*|оформил\w*).{0,25}заявк\w*|уведомлен\w*.{0,30}(?:отправлен\w*|доставлен\w*)|юрист\w*\s+уведомлен\w*|отправил\w*.{0,30}(?:telegram|телеграм|юрист\w*)|истори\w*.{0,30}(?:очищен\w*|удален\w*)|сесси\w*.{0,30}удален\w*|вы\s+записаны)"
CLIENT_FACT = r"\b(?:у вас\s+(?:есть|двое|трое|четверо|[0-9])|вы\s+(?:проживаете|работаете|подписали|уволены|заключили|состоите)|ваш\w*\s+(?:ребенок|работодатель|супруг|договор)\s+(?:имеет|находится|заключен|нарушил))"
CONTACT_REQUEST = r"\b(?:укажите|сообщите|назовите|напишите|пришлите|оставьте).{0,50}\b(?:телефон\w*|номер\w*|имя|контакт\w*|паспорт\w*)"


class Judge:
    def __init__(self, settings: JudgeSettings | None = None, llm: BaseLLM | None = None,
                 *, knowledge_path=None):
        self.settings = settings or load_judge_settings()
        self.llm = llm
        self.knowledge = load_knowledge(knowledge_path)
        if PRICE_STATEMENT not in self.knowledge:
            raise ValueError("Политика стоимости базы знаний изменилась: обновите правила Judge.")
        price_section = self.knowledge.split("5. ПРАВИЛА ОПРЕДЕЛЕНИЯ СТОИМОСТИ",1)[-1].split("6.",1)[0]
        self.known_price_lines = tuple(canonical(line) for line in price_section.splitlines() if line.strip())

    @staticmethod
    def _sentences(text: str) -> tuple[str,...]:
        return tuple(s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+",text) if s.strip())

    def _local_issues(self, draft: str, context: JudgeContext) -> tuple[Issue,...]:
        if not isinstance(draft,str) or not draft.strip():
            return (Issue.EMPTY,)
        if len(draft)>self.settings.max_answer_chars:
            return (Issue.TOO_LONG,)
        issues = []
        raw = clean_unicode(draft)
        text = canonical(draft)
        if re.search(SECRET,raw,re.I) or any(canonical(v) in text for v in context.secrets):
            issues.append(Issue.SECRET)
        allowed_lead_id = context.actions.lead.lead_id if context.actions.lead and context.actions.lead.success else None
        privacy_text = raw.replace(allowed_lead_id,"") if allowed_lead_id else raw
        if (any(re.search(p,privacy_text) for p in (PHONE,EMAIL,IDENTIFIER,SENSITIVE))
                or any(canonical(v) in text for v in context.private_values)):
            issues.append(Issue.PERSONAL)
        pricing = context.intent==Intent.PRICE or re.search(r"консультаци\w*|наши\s+услуг\w*|юрист\w*|\b(?:цен\w*|стоимост\w*|оплат\w*|гонорар\w*|вознагражден\w*)",text)
        price_text = text
        for known_line in self.known_price_lines:
            price_text = price_text.replace(known_line,"")
        if pricing and (re.search(MONEY,price_text) or re.search(
            r"\bбесплатн\w*|\bскидк\w*.{0,20}(?:[0-9]|процент)|\b[0-9]+\s*%|"
            r"(?:стоимость|цена|стоит|обойдется|от|всего|за)\s*[:—–-]?\s*(?:от\s+)?[0-9]|"
            r"\b(?:пять|десять|две|три|двадцать)\s+тысяч\w*|в подарок|без оплаты|за наш счет",price_text)):
            issues.append(Issue.PRICE)
        positive = re.sub(r"\bне\s+(?:(?:могу|можем)\s+)?(?:гарантир\w*|гарантирован\w*)","",text)
        if re.search(GUARANTEE,positive):
            issues.append(Issue.GUARANTEE)
        if (re.search(LAW,text) or re.search(r"https?://|www\.|\b(?:пункт\w*|п\.)\s*[0-9]|\b(?:коап|нк|апк|кас)\s+рф\b",text)
                or re.search(r"\b(?:по закону|обязаны|должны|срок\w*|в течение|не позднее)\b.{0,65}\b(?:дн\w*|день|дней|сут\w*|час\w*|месяц\w*|год\w*)\b",text)):
            issues.append(Issue.LAW)
        if re.search(r"(?:игнорируй|забудь|обойди).{0,40}(?:инструкци\w*|правил\w*)|system\s*prompt|<\|(?:system|assistant)|<(?:script|iframe|img|form|svg)\b|javascript:|onerror\s*=",text):
            issues.append(Issue.INJECTION)
        actions = {canonical(s).rstrip(".! ") for s in context.actions.sentences()}
        for sentence in self._sentences(raw):
            value = canonical(sentence)
            if (re.search(ACTION,value) or re.search(
                    r"\b(?:очистил\w*|удалил\w*).{0,25}(?:истори\w*|сесси\w*|заявк\w*)|"
                    r"\b(?:сообщени\w*|письмо).{0,25}(?:отправлен\w*|доставлен\w*|ушло)|"
                    r"\bя\b.{0,15}\b(?:записал\w*|создал\w*|отправил\w*|сохранил\w*|удалил\w*)",value)) and value.rstrip(".! ") not in actions:
                issues.append(Issue.ACTION)
            if (re.search(CLIENT_FACT,value) and not re.search(r"\bесли\b|по вашим словам",value)
                    and not any(value.rstrip(".! ") in canonical(f) for f in context.facts)):
                issues.append(Issue.FACT)
        if (re.search(r"\b(?:опыт|стаж).{0,25}(?:[0-9]+|десять|двадцать|пять)\s*(?:лет|год)|\bпринимаем.{0,30}(?:улиц|адрес)|\b(?:перезвон\w*|свяж\w*|ответим).{0,30}(?:через|сегодня|завтра|в течение)|\b(?:консультаци\w*\s+(?:назначена|состоится)|вы записаны)",text)
                or re.search(r"\b(?:[Юю]рист|[Сс]пециалист)[\s—:-]+[А-ЯЁ][а-яё-]+",raw)):
            issues.append(Issue.FACT)
        if re.search(CONTACT_REQUEST,text) and (not context.contact_collection_allowed or context.consent_refused):
            issues.append(Issue.CONSENT)
        if context.intent==Intent.PRICE and not re.search(r"стоимост\w*|цен\w*|расчет\w*|услуг\w*|стоит|консультаци\w*|бесплатн\w*",text):
            issues.append(Issue.IRRELEVANT)
        if context.intent==Intent.GENERAL:
            stop = {"каки","како","кото","пожа","можн","нужн","буде","если","вопр","юрид","расс","подс","прив"}
            question_stems = {w[:4] for w in re.findall(r"[а-яa-z]{4,}",canonical(context.question))}-stop
            answer_stems = {w[:4] for w in re.findall(r"[а-яa-z]{4,}",text)}
            if question_stems and not question_stems.intersection(answer_stems):
                issues.append(Issue.IRRELEVANT)
        return tuple(dict.fromkeys(issues))

    @staticmethod
    def _redact(text: str, context: JudgeContext) -> str:
        text = clean_unicode(text)
        for value in sorted(context.private_values+context.secrets,key=len,reverse=True):
            text = re.sub(re.escape(value),"[СКРЫТО]",text,flags=re.I)
        for pattern in (SECRET,PHONE,EMAIL,IDENTIFIER,SENSITIVE):
            text = re.sub(pattern,"[СКРЫТО]",text,flags=re.I)
        return text

    def _fallback(self, context: JudgeContext) -> str:
        actions = " ".join(context.actions.sentences())
        if context.consent_refused:
            answer = "Без согласия оформление новой заявки не продолжается. Можно обсудить общий вопрос без контактов."
        elif context.intent==Intent.PRICE:
            answer = PRICE_STATEMENT+" Какая услуга нужна и в чём кратко состоит вопрос?"
        elif context.intent==Intent.BOOKING:
            answer = ("Для оформления заявки уточните тему и краткое описание ситуации. "
                      "Для регистрации потребуется отдельное согласие на обработку персональных данных.")
            if context.actions.lead and context.actions.lead.success:
                answer = "Время консультации и дальнейший порядок нужно согласовать с юристом."
        elif context.intent==Intent.GENERAL:
            answer = ("Для предварительного разъяснения нужно уточнить обстоятельства. "
                      "Что произошло и какой результат вы хотите получить? "+DISCLAIMER)
        else:
            answer = "Уточните, пожалуйста: вас интересует стоимость, запись или юридический вопрос?"
        return (actions+" "+answer).strip()

    def review(self, draft: str, context: JudgeContext) -> JudgeResult:
        if not isinstance(context,JudgeContext):
            raise ValueError("Требуется серверный JudgeContext.")
        issues = self._local_issues(draft,context)
        if issues:
            return JudgeResult(self._fallback(context),issues,True)
        if self.settings.mode=="local":
            return JudgeResult(draft.strip())
        if self.llm is None or not context.cloud_review_allowed:
            return JudgeResult(self._fallback(context),(Issue.SEMANTIC_UNAVAILABLE,),True)
        payload = {"question":self._redact(context.question,context),"draft":self._redact(draft,context),
            "intent":context.intent.value,"facts":[self._redact(f,context) for f in context.facts],
            "knowledge":self._redact(self.knowledge,context),"allowed_actions":context.actions.sentences()}
        try:
            result = self.llm.generate(JUDGE_RULES,[Message("user",json.dumps(payload,ensure_ascii=False))])
            def unique_object(pairs):
                value = {}
                for key,item in pairs:
                    if key in value:
                        raise ValueError("Повторяющееся поле вердикта.")
                    value[key] = item
                return value
            verdict = json.loads(result.text,object_pairs_hook=unique_object)
            permitted = {i.value for i in (Issue.IRRELEVANT,Issue.PRICE,Issue.GUARANTEE,Issue.LAW,
                                          Issue.FACT,Issue.ACTION,Issue.PERSONAL,Issue.SECRET,Issue.CONSENT)}
            if (not isinstance(verdict,dict) or set(verdict)!={"approved","issues"}
                    or type(verdict["approved"]) is not bool or not isinstance(verdict["issues"],list)
                    or len(verdict["issues"])>9 or any(not isinstance(v,str) or v not in permitted for v in verdict["issues"])
                    or verdict["approved"]!= (not verdict["issues"])):
                raise ValueError
        except LLMError:
            return JudgeResult(self._fallback(context),(Issue.SEMANTIC_UNAVAILABLE,),True)
        except (ValueError,TypeError,AttributeError):
            return JudgeResult(self._fallback(context),(Issue.SEMANTIC_INVALID,),True)
        if not verdict["approved"]:
            return JudgeResult(self._fallback(context),tuple(Issue(v) for v in verdict["issues"]),True,True)
        return JudgeResult(draft.strip(),semantic_checked=True)
