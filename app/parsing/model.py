"""Структуры для разбора документов."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


class ParseError(Exception):
    """Документ нельзя разобрать. Текст ошибки показывается пользователю."""


@dataclass
class Block:
    """Строка или абзац исходного документа до разбивки на пункты."""

    text: str
    page: int | None = None
    heading_level: int = 0  # 0 = обычный текст, 1..6 = заголовок
    is_table_row: bool = False
    source: str = ""  # например "табл. 2" или "лист «Лимиты»"


@dataclass
class Clause:
    doc_title: str
    clause_no: str
    section: str
    page: int | None
    text: str
    kind: str = "clause"  # clause | paragraph | table_row
    part: int = 0  # номер части, если длинный пункт разрезан
    ord: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def text_hash(self) -> str:
        raw = f"{self.clause_no}\x1f{self.section}\x1f{self.page}\x1f{self.part}\x1f{self.text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @property
    def ref(self) -> str:
        """Ссылка вида «Положение об отпусках, п. 3.1»."""
        return format_ref(self.doc_title, self.clause_no, self.page, self.kind)


def format_ref(doc_title: str, clause_no: str, page: int | None = None, kind: str = "clause") -> str:
    parts = [doc_title]
    if clause_no:
        parts.append(clause_no if kind == "table_row" else f"п. {clause_no}")
    if page:
        parts.append(f"стр. {page}")
    return ", ".join(parts)
