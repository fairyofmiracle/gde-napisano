"""Превращение последовательности блоков в пункты с номерами и разделами."""
from __future__ import annotations

import re
from collections.abc import Iterable

from .model import Block, Clause

# 1.  / 1.2.  / 1.2.3 / 3) — номер пункта в начале строки
_CLAUSE_RE = re.compile(
    r"^\s*(?:п\.\s*|пункт\s+)?(\d{1,2}(?:\.\d{1,3}){0,4})([.)]|\s)\s*(\S.*)$", re.IGNORECASE
)
_SECTION_WORD_RE = re.compile(
    r"^\s*(раздел|глава|статья|часть)\s+([0-9IVXLC]+)\.?\s*(.*)$", re.IGNORECASE
)
_PAGE_NUM_RE = re.compile(r"^\s*[-–—]?\s*\d{1,4}\s*[-–—]?\s*$")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+(?=[А-ЯЁA-Z0-9«\"(])")


def _looks_like_heading(text: str) -> bool:
    t = text.strip()
    return 0 < len(t) <= 90 and not t.endswith((".", ";", ",", ":")) and len(t.split()) <= 12


def _clean(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u00ad", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    sentences = _SENT_SPLIT_RE.split(text)
    parts: list[str] = []
    cur = ""
    for s in sentences:
        while len(s) > limit:  # очень длинное «предложение» режем по словам
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + 1 + len(s) > limit:
            parts.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        parts.append(cur)
    return parts


def build_clauses(blocks: Iterable[Block], doc_title: str, max_chars: int = 1200) -> list[Clause]:
    clauses: list[Clause] = []
    section = ""
    cur: dict | None = None
    last_no = ""

    def flush() -> None:
        nonlocal cur
        if cur and cur["lines"]:
            text = _join_lines(cur["lines"])
            if text:
                for i, piece in enumerate(split_long(text, max_chars)):
                    clauses.append(
                        Clause(
                            doc_title=doc_title,
                            clause_no=cur["no"],
                            section=cur["section"],
                            page=cur["page"],
                            text=piece,
                            kind=cur["kind"],
                            part=i,
                        )
                    )
        cur = None

    blocks = list(blocks)
    for idx, b in enumerate(blocks):
        text = _clean(b.text)
        if not text or _PAGE_NUM_RE.match(text):
            continue

        if b.is_table_row:
            flush()
            label = b.source or "таблица"
            no = f"{label}, к п. {last_no}" if last_no else label
            clauses.append(
                Clause(doc_title, no, section, b.page, text, kind="table_row")
            )
            continue

        if b.heading_level:
            flush()
            section = text
            continue

        m_sec = _SECTION_WORD_RE.match(text)
        if m_sec and _looks_like_heading(text):
            flush()
            section = text
            continue

        m = _CLAUSE_RE.match(text)
        if not m and _looks_like_heading(text) and _next_is_clause(blocks, idx):
            prev_text = (cur["lines"][-1] if cur and cur["lines"] else "")
            if not prev_text or prev_text.rstrip().endswith((".", ";", ":", "!", "?")):
                flush()
                section = text
                continue
        if m:
            no, delim, rest = m.group(1), m.group(2), m.group(3)
        if m and (
            ("." not in no and delim.isspace())  # «28 календарных дней» в переносе строки
            or (delim == ")" and cur is not None and cur["no"])  # подпункт «1)» внутри пункта
        ):
            m = None
        if m:
            # «3. Ежегодный отпуск» — заголовок раздела, а не пункт
            if "." not in no and _looks_like_heading(rest) and not rest[:1].islower():
                flush()
                section = f"{no}. {rest}"
                last_no = no
                continue
            flush()
            last_no = no
            cur = {"no": no, "section": section, "page": b.page, "lines": [rest], "kind": "clause"}
            continue

        if cur is None:
            cur = {"no": "", "section": section, "page": b.page, "lines": [], "kind": "paragraph"}
        cur["lines"].append(text)
        # без номера: каждый абзац верхнего уровня — отдельный кусок, если он уже длинный
        if cur["kind"] == "paragraph" and sum(len(x) for x in cur["lines"]) > max_chars // 2:
            flush()
    flush()

    for i, c in enumerate(clauses):
        c.ord = i
    return clauses


def _next_is_clause(blocks: list[Block], idx: int) -> bool:
    for nb in blocks[idx + 1 : idx + 3]:
        t = _clean(nb.text)
        if not t:
            continue
        m = _CLAUSE_RE.match(t)
        return bool(m and ("." in m.group(1) or m.group(2) in ".)"))
    return False


def _join_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        if out.endswith("-") and line[:1].islower():
            out = out[:-1] + line  # перенос слова: «слу-» + «жебной»
        elif out:
            out += " " + line
        else:
            out = line
    return out.strip()


def sub_item_labels(rows: Iterable[tuple[str, str, str, str]]) -> dict[str, str]:
    """Метки подпунктов для отображения.

    Нумерованный список внутри пункта («1.3. … то: 1. … 2. …») разбирается как отдельные
    пункты с номерами «1», «2». Номер в базе не меняется (от него зависит point_id),
    а для ссылок подпункту ставится метка «1.3, подп. 1».

    rows: (point_id, kind, section, clause_no) в порядке следования в документе.
    Возвращает {point_id: метка} только для подпунктов.
    """
    out: dict[str, str] = {}
    parent = ""
    section = None
    for point_id, kind, sec, no in rows:
        if sec != section:
            section, parent = sec, ""
        if kind != "clause" or not no:
            continue
        if "." in no:
            parent = no
        elif no.isdigit() and parent:
            out[point_id] = f"{parent}, подп. {no}"
    return out
