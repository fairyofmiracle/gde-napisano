"""Разбор документов на пункты."""
from __future__ import annotations

import re
from pathlib import Path

from .model import Block, Clause, ParseError, format_ref
from .structure import build_clauses

SUPPORTED_EXTENSIONS = {
    ".docx", ".doc", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp",
    ".txt", ".md", ".rtf", ".odt", ".xlsx", ".html", ".htm",
}

__all__ = ["Block", "Clause", "ParseError", "SUPPORTED_EXTENSIONS", "parse_file", "doc_title_from_path", "format_ref"]


def doc_title_from_path(path: Path) -> str:
    stem = re.sub(r"[_]+", " ", path.stem).strip()
    return stem[:1].upper() + stem[1:] if stem else path.name


def parse_blocks(path: Path, settings) -> list[Block]:
    ext = path.suffix.lower()
    if ext == ".docx":
        from .docx_parser import parse_docx

        return parse_docx(path)
    if ext == ".doc":
        from .docx_parser import parse_docx
        from .other import convert_doc

        converted, tmp = convert_doc(path, settings.soffice_cmd)
        try:
            return parse_docx(converted)
        finally:
            tmp.cleanup()
    if ext == ".pdf":
        from .pdf_parser import parse_pdf

        return parse_pdf(path, settings)
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        from .pdf_parser import parse_image

        return parse_image(path, settings)
    if ext in (".txt", ".md"):
        from .other import parse_txt

        return parse_txt(path)
    if ext == ".rtf":
        from .other import parse_rtf

        return parse_rtf(path)
    if ext == ".odt":
        from .other import parse_odt

        return parse_odt(path)
    if ext == ".xlsx":
        from .other import parse_xlsx

        return parse_xlsx(path)
    if ext in (".html", ".htm"):
        from .other import parse_html

        return parse_html(path)
    raise ParseError(f"Формат {ext or 'без расширения'} не поддерживается")


def parse_file(path: Path, settings, title: str | None = None) -> list[Clause]:
    blocks = parse_blocks(path, settings)
    clauses = build_clauses(blocks, title or doc_title_from_path(path), settings.max_clause_chars)
    if not clauses:
        raise ParseError("В документе не найден текст (пустой файл или не распознан скан)")
    return clauses
