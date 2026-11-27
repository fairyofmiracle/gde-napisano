"""Пересказ ответа простыми словами: те же пункты и цитаты, без нового поиска."""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from app.answer import parse_json, unsupported_numbers
from app.llm import LLMError
from app.llm.prompts import SIMPLIFY_SYSTEM, neutralize, simplify_user

log = logging.getLogger(__name__)

_WORD = r"0-9A-Za-zА-Яа-яЁё"


def can_simplify(out: dict | None) -> bool:
    """Кнопка «Не понял» только у ответа по регламенту с цитатами, без эскалации."""
    if not out:
        return False
    if out.get("mode") != "confident":
        return False
    if out.get("escalation") or out.get("escalated"):
        return False
    if out.get("answer_kind") == "quote_only":
        return False
    if not out.get("citations"):
        return False
    return bool(str(out.get("text") or "").strip())


def payload_from_answer(out: dict) -> dict:
    """Сохраняемые поля, без почты и хода работы."""
    citations = []
    for c in out.get("citations") or []:
        citations.append({k: c.get(k) for k in (
            "n", "point_id", "doc_id", "doc_title", "clause_no", "page", "ref", "quote",
            "verified", "auto_quote", "box", "doc_date", "link") if c.get(k) is not None})
    glossary = [{"abbr": g.get("abbr", ""), "full": g.get("full", "")}
                for g in (out.get("glossary") or []) if g.get("abbr") and g.get("full")]
    return {
        "question": out.get("question") or "",
        "mode": out.get("mode") or "",
        "text": out.get("text") or "",
        "citations": citations,
        "glossary": glossary,
        "advice": out.get("advice") or "",
        "answer_kind": out.get("answer_kind"),
        "escalated": bool(out.get("escalation") or out.get("escalated")),
    }


def expand_abbreviations(text: str, terms: list[dict]) -> str:
    """Подставляет к сокращению расшифровку из глоссария: «ПА (пресейл-архитектор)», без повтора."""
    out = text or ""
    items = sorted((t for t in (terms or []) if t.get("abbr") and t.get("full")),
                   key=lambda t: -len(t["abbr"]))
    for t in items:
        abbr, full = t["abbr"], t["full"]
        already = re.compile(rf"(?<![{_WORD}]){re.escape(abbr)}(?![{_WORD}])\s*\(\s*{re.escape(full)}\s*\)", re.I)
        if already.search(out):
            continue
        pat = re.compile(rf"(?<![{_WORD}]){re.escape(abbr)}(?![{_WORD}])", re.I)

        def _repl(m, f=full, src=None):
            start = m.start()
            if src[:start].count("(") > src[:start].count(")"):
                return m.group(0)
            return f"{m.group(0)} ({f})"

        out = pat.sub(lambda m, f=full, src=out: _repl(m, f, src), out)
    return out


def _clean_refs(text: str, n_max: int) -> str:
    return re.sub(r"\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= n_max else "", text or "").strip()


def _sources(citations: list[dict], extra: list[str], clause_texts: dict[str, str] | None = None) -> list:
    out = list(extra)
    clause_texts = clause_texts or {}
    for c in citations or []:
        text = " ".join(x for x in (
            c.get("quote") or "", c.get("ref") or "", clause_texts.get(c.get("point_id") or "") or "",
        ) if x)
        out.append(SimpleNamespace(
            doc_title=c.get("doc_title") or "",
            clause_no=c.get("clause_no") or "",
            section="",
            text=text,
        ))
    return out


def _clause_texts(db, citations: list[dict]) -> dict[str, str]:
    ids = [c.get("point_id") for c in citations or [] if c.get("point_id")]
    if not db or not ids:
        return {}
    q = f"SELECT point_id, text FROM clauses WHERE point_id IN ({','.join('?' * len(ids))})"
    return {r["point_id"]: r["text"] for r in db.query(q, tuple(ids))}


def simplify(llm, payload: dict, *, db=None) -> dict:
    """Пересказывает payload['text'] простыми словами. Цитаты возвращает без изменений.

    Если модель недоступна или в тексте появились новые числа — возвращается исходный ответ
    с раскрытыми сокращениями.
    """
    citations = list(payload.get("citations") or [])
    glossary = list(payload.get("glossary") or [])
    original = str(payload.get("text") or "").strip()
    question = str(payload.get("question") or "").strip()
    advice = str(payload.get("advice") or "").strip()
    fallback = expand_abbreviations(original, glossary)
    fallback_advice = expand_abbreviations(advice, glossary) if advice else ""
    result = {
        "text": fallback,
        "advice": fallback_advice,
        "citations": citations,
        "glossary": glossary,
        "question": question,
        "simplified": fallback != original,
        "source": "fallback",
    }
    if not original or not citations:
        return result
    clause_texts = _clause_texts(db, citations)
    allowed = _sources(citations, [original, advice], clause_texts)
    n_max = max((int(c.get("n") or 0) for c in citations), default=0)
    try:
        r = llm.chat(SIMPLIFY_SYSTEM, simplify_user(question, original, citations, glossary), json_mode=True)
        data = parse_json(r.text)
        text = _clean_refs(str(data.get("text") or "").strip(), n_max or 99)
    except (LLMError, ValueError, TypeError) as e:
        log.info("пересказ не получен: %s", type(e).__name__)
        return result
    if not text or len(text) < 8:
        return result
    bad = unsupported_numbers(text, allowed)
    if bad:
        log.info("пересказ скрыт: новые числа %s", ", ".join(bad[:5]))
        return result
    result["text"] = text
    result["simplified"] = True
    result["source"] = "model"
    return result
