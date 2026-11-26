from __future__ import annotations

from pathlib import Path

import pytest

from app.parsing import ParseError, parse_file
from app.parsing.structure import build_clauses, split_long
from app.parsing.model import Block
from tests.conftest import has_soffice, has_tesseract

CASES = [
    ("hr/Положение_об_отпусках.docx", ["1.1", "2.1", "3.1", "3.2", "3.3", "3.4", "3.5", "4.1", "4.2", "5.1", "5.2"]),
    ("it/Регламент_предоставления_доступов.docx", ["1.1", "1.2", "1.3", "1.4"]),
    ("hr/ПВТР.pdf", ["1.1", "2.1", "2.2", "2.3", "3.1", "3.2", "4.1"]),
    ("hr/Регламент_оформления_командировок.rtf", ["1.1", "1.2", "1.3", "2.1", "2.2", "3.1"]),
    ("hr/Положение_об_удалённой_работе.odt", ["1.1", "1.2", "1.3", "1.4"]),
    ("it/Инструкция_пользователя_ИТ.md", ["2.1", "2.2", "2.3"]),
    ("it/Регламент_удалённого_доступа.txt", ["1.1", "1.2", "1.3"]),
    ("it/Регламент_установки_ПО.html", ["3.1", "3.2", "3.3"]),
    ("finance/Положение_об_оплате_труда.xlsx", ["4.1", "4.2", "4.3"]),
]
OCR_CASES = [
    ("it/Регламент_ИТ-поддержки.pdf", ["1.1", "1.2", "1.3"]),
    ("finance/Регламент_выдачи_финансовых_справок.png", ["1.1", "1.2"]),
]


@pytest.mark.parametrize("rel,expected", CASES)
def test_formats_keep_clause_numbers(sample_docs, settings, rel, expected):
    clauses = parse_file(sample_docs / rel, settings)
    assert [c.clause_no for c in clauses] == expected
    assert all(c.doc_title for c in clauses)
    assert all(c.text and not c.text[:3].strip(".").isdigit() for c in clauses)


@pytest.mark.skipif(not has_tesseract(), reason="нет tesseract")
@pytest.mark.parametrize("rel,expected", OCR_CASES)
def test_scans_and_images_via_ocr(sample_docs, settings, rel, expected):
    clauses = parse_file(sample_docs / rel, settings)
    assert [c.clause_no for c in clauses] == expected
    assert all(c.page == 1 for c in clauses)
    text = " ".join(c.text for c in clauses)
    assert "рабоч" in text


def test_docx_word_autonumbering_restored(sample_docs, settings):
    import docx

    path = sample_docs / "hr/Положение_об_отпусках.docx"
    raw = [p.text for p in docx.Document(str(path)).paragraphs]
    assert not any(t.startswith("3.1") for t in raw), "номера должны быть только в автонумерации Word"
    clauses = parse_file(path, settings)
    c = next(c for c in clauses if c.clause_no == "3.1")
    assert "28 календарных дней" in c.text
    assert c.section == "3. Ежегодный оплачиваемый отпуск"
    assert c.ref.startswith("Положение об отпусках, п. 3.1")


def test_pdf_keeps_page_numbers(sample_docs, settings):
    clauses = parse_file(sample_docs / "hr/ПВТР.pdf", settings)
    assert all(c.page == 1 for c in clauses)
    assert "стр. 1" in clauses[0].ref


@pytest.mark.skipif(not has_soffice(), reason="нет LibreOffice")
def test_doc_via_libreoffice(sample_docs, settings):
    clauses = parse_file(sample_docs / "finance/Порядок_авансовых_отчётов.doc", settings)
    assert [c.clause_no for c in clauses] == ["2.1", "2.2", "2.3"]


def test_doc_without_libreoffice_gives_clear_error(sample_docs, tmp_path):
    from tests.conftest import make_settings

    s = make_settings(tmp_path, soffice_cmd="no-such-soffice")
    src = sample_docs / "finance/Порядок_авансовых_отчётов.doc"
    if not src.exists():
        src = tmp_path / "x.doc"
        src.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(ParseError, match="LibreOffice"):
        parse_file(src, s)


def test_unsupported_and_empty(tmp_path, settings):
    bad = tmp_path / "x.csv"
    bad.write_text("a,b")
    with pytest.raises(ParseError, match="не поддерживается"):
        parse_file(bad, settings)
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n")
    with pytest.raises(ParseError):
        parse_file(empty, settings)


def test_structure_rules():
    blocks = [Block(x) for x in [
        "Регламент", "1. Общие положения", "1.1. Первый пункт продолжается", "на следующей строке и пере-",
        "носом.", "1) подпункт внутри", "28 календарных дней в переносе.", "Порядок оплаты", "2.1 Второй пункт.", "— 3 —",
    ]]
    cl = build_clauses(blocks, "Док")
    assert [c.clause_no for c in cl] == ["1.1", "2.1"]
    assert cl[0].section == "1. Общие положения"
    assert "переносом." in cl[0].text and "1) подпункт" in cl[0].text and "28 календарных" in cl[0].text
    assert cl[1].section == "Порядок оплаты"


def test_long_clause_split_keeps_number():
    text = " ".join(f"Предложение номер {i} достаточно длинное." for i in range(80))
    cl = build_clauses([Block(f"4.2. {text}")], "Док", max_chars=300)
    assert len(cl) > 1 and {c.clause_no for c in cl} == {"4.2"}
    assert [c.part for c in cl] == list(range(len(cl)))
    assert all(len(p) <= 300 for p in split_long(text, 300))
