"""Фильтр вопросов вне базы регламентов и поиск ссылок на документы, которых нет в базе."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.text import tokens

# Личная мотивация и деньги по сделке: это вопрос к руководителю, не к регламентам
_MONEY = re.compile(r"\b(марж\w*|марш\w*|преми\w*|бонус\w*|зарплат\w*|оклад\w*|мотиваци\w*|kpi)\b", re.I)
_PERSONAL = re.compile(r"\b(мо[йяеию]\w*|мне|у\s+меня|я)\b", re.I)
_DEAL = re.compile(r"\b(сделк\w*|продал\w*|закрыл\w*|клиент\w*)\b", re.I)
# Просьбы сгенерировать документ или анализ — не поиск по регламентам
_GENERATE = re.compile(
    r"(follow[\s-]?up|фол+оу?\s*ап|фол+ап|фуллап)"
    r"|\b(сделай|составь|напиши|подготовь|сочини|придумай|переведи|проанализируй|сгенерируй)\b"
    r"|\bсделать\s+(анализ|письмо|отчёт|отчет|резюме|протокол)", re.I)
_DOC_LOOKUP = re.compile(r"\b(где|какой|какие|кто|когда|сколько|можно|нужно|должен|обязан|порядок|пункт|регламент)\w*",
                         re.I)
_SMALLTALK = re.compile(
    r"^\s*(привет\w*|здравств\w*|добр\w+\s+(день|утро|вечер)|как\s+дела|спасибо\w*|пока|кто\s+ты|ты\s+кто)\W*$"
    r"|курс\s+(доллара|евро|валют)|\bпогод\w*|\bанекдот\w*|\bгороскоп\w*", re.I)

MSG_MANAGER = ("Этот вопрос не относится к корпоративным регламентам в базе знаний, поэтому ответить на него "
               "я не могу. Обратитесь к своему руководителю.")
MSG_GENERATE = ("Я ищу ответы только в корпоративных регламентах и не составляю письма, анализы и другие документы. "
                "Если нужен порядок действий по регламенту, спросите, что в нём написано, и я покажу пункт.")
MSG_SMALLTALK = ("Я отвечаю на вопросы по корпоративным регламентам: проектная и пресейловая деятельность "
                 "ДБРЦТ, Дирекции ИБ и Технической дирекции. Спросите, например: «Кто отвечает за договор "
                 "с субподрядчиком?»")


@dataclass
class OffTopic:
    kind: str  # money | generate | smalltalk
    text: str


def off_topic(question: str) -> OffTopic | None:
    q = question or ""
    if _SMALLTALK.search(q):
        return OffTopic("smalltalk", MSG_SMALLTALK)
    if _MONEY.search(q) and (_PERSONAL.search(q) or _DEAL.search(q)):
        return OffTopic("money", MSG_MANAGER)
    if _GENERATE.search(q):
        # «Напиши, где сказано про смету» — это всё-таки поиск по регламенту
        if not re.search(r"\b(где|какой|каком|пункт\w*)\b", q, re.I):
            return OffTopic("generate", MSG_GENERATE)
    return None


# Упоминание другого документа: «регламент подбора субподрядчика», «регламентом привлечения подрядчиков…»
_DOC_REF = re.compile(
    r"\b((?:регламент|положени|инструкци|политик|стандарт|методик|порядк)\w*"
    r"\s+(?:(?:по|о|об|на|для)\s+)?[А-Яа-яЁё][А-Яа-яЁё\-]+(?:\s+(?:на|по|для|с|и)?\s*[А-Яа-яЁё][А-Яа-яЁё\-]+){0,5})",
    re.I)
_STOP_TAIL = {"в", "и", "с", "на", "по", "для", "который", "которые", "если", "при", "после", "до"}
_GENERIC = {"регламент", "регламента", "регламенту", "регламентом", "регламенте", "регламенты", "регламентов",
            "положение", "порядок", "порядка"}


_NOMINATIVE = [("регламент", "Регламент"), ("положени", "Положение"), ("инструкци", "Инструкция"),
               ("политик", "Политика"), ("стандарт", "Стандарт"), ("методик", "Методика"), ("порядк", "Порядок")]


def _clean_name(name: str) -> str:
    words = name.split()
    while words and words[-1].lower() in _STOP_TAIL:
        words.pop()
    if words:
        head = words[0].lower()
        for stem_, nom in _NOMINATIVE:
            if head.startswith(stem_):
                words[0] = nom
                break
    return " ".join(words)


# В тексте пунктов ловим только явную отсылку: «согласно / в соответствии с / по регламенту …»
_CLAUSE_REF = re.compile(
    r"(?:согласно|соответствии\s+с|по|см\.?)\s+((?:регламент|положени)\w*\s+(?:о\s+|об\s+|по\s+)?"
    r"[А-Яа-яЁё][А-Яа-яЁё\-]+(?:\s+(?:на|по|для|с|и)?\s*[А-Яа-яЁё][А-Яа-яЁё\-]+){0,4})", re.I)
_NOT_A_DOC = re.compile(r"^\S+\s+(заказчик\w*|под|при|настоящ\w*|компани\w*)\b", re.I)


def doc_mentions(text: str, strict: bool = False) -> list[str]:
    out = []
    rx = _CLAUSE_REF if strict else _DOC_REF
    for m in rx.finditer(text or ""):
        name = _clean_name(m.group(1))
        if len(name.split()) >= 2 and name.lower() not in _GENERIC and not _NOT_A_DOC.search(name):
            out.append(name)
    return out


def missing_documents(text: str, titles: list[str], min_share: float = 0.6, strict: bool = False) -> list[str]:
    """Названия документов из текста, которых нет среди загруженных (по доле общих основ слов)."""
    title_stems = [set(tokens(t.replace("_", " "))) for t in titles]
    stop = set(tokens("регламент положение инструкция политика стандарт методика порядок деятельность"))
    out: list[str] = []
    for name in doc_mentions(text, strict):
        st = set(tokens(name)) - stop
        if not st:
            continue
        best = max((len(st & ts) / len(st) for ts in title_stems), default=0.0)
        if best < min_share and name not in out:
            out.append(name)
    return out
