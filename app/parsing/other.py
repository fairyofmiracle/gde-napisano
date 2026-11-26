"""txt/md, rtf, odt, xlsx, html, doc."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .model import Block, ParseError


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_txt(path: Path) -> list[Block]:
    blocks = []
    for line in read_text_file(path).splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            blocks.append(Block(m.group(2), heading_level=len(m.group(1))))
        elif line.strip():
            blocks.append(Block(re.sub(r"^\s*[-*]\s+", "", line)))
    return blocks


def parse_rtf(path: Path) -> list[Block]:
    try:
        from striprtf.striprtf import rtf_to_text
    except ImportError as e:  # pragma: no cover
        raise ParseError("Не установлен striprtf") from e
    text = rtf_to_text(read_text_file(path), errors="ignore")
    return [Block(line) for line in text.splitlines() if line.strip()]


def parse_odt(path: Path) -> list[Block]:
    try:
        from odf import teletype, text as odf_text
        from odf.namespaces import TABLENS, TEXTNS
        from odf.opendocument import load
    except ImportError as e:  # pragma: no cover
        raise ParseError("Не установлен odfpy") from e
    try:
        doc = load(str(path))
    except Exception as e:
        raise ParseError(f"Не удалось открыть .odt: {e}") from e

    numbered_styles = set()
    for style in doc.getElementsByType(odf_text.ListStyle):
        for child in style.childNodes:
            if child.qname == (TEXTNS, "list-level-style-number"):
                numbered_styles.add(style.getAttribute("name"))
                break

    blocks: list[Block] = []

    def walk(node, counters: list[int], numbered: bool) -> None:
        for child in node.childNodes:
            q = getattr(child, "qname", None)
            if q == (TEXTNS, "h"):
                lvl = int(child.getAttribute("outlinelevel") or 1)
                blocks.append(Block(teletype.extractText(child), heading_level=lvl))
            elif q == (TEXTNS, "p"):
                blocks.append(Block(teletype.extractText(child)))
            elif q == (TEXTNS, "list"):
                style = child.getAttribute("stylename") or ""
                walk(child, counters + [0], numbered or style in numbered_styles)
            elif q == (TEXTNS, "list-item"):
                counters[-1] += 1
                label = ".".join(str(c) for c in counters) + "." if numbered else ""
                for sub in child.childNodes:
                    sq = getattr(sub, "qname", None)
                    if sq in ((TEXTNS, "p"), (TEXTNS, "h")):
                        t = teletype.extractText(sub)
                        if t.strip():
                            blocks.append(Block(f"{label} {t}".strip()))
                            label = ""
                    elif sq == (TEXTNS, "list"):
                        walk_list(sub, counters, numbered)
            elif q == (TABLENS, "table-row"):
                cells = [" ".join(teletype.extractText(c).split()) for c in child.childNodes
                         if getattr(c, "qname", None) == (TABLENS, "table-cell")]
                if any(cells):
                    blocks.append(Block(" | ".join(x for x in cells if x), is_table_row=True, source="таблица"))
            elif hasattr(child, "childNodes") and q not in ((TEXTNS, "tracked-changes"),):
                walk(child, counters, numbered)

    def walk_list(lst, counters: list[int], numbered: bool) -> None:
        wrapper = type("W", (), {"childNodes": [lst]})
        walk(wrapper, counters, numbered)

    walk(doc.text, [], False)
    return blocks


def parse_xlsx(path: Path) -> list[Block]:
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover
        raise ParseError("Не установлен openpyxl") from e
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:
        raise ParseError(f"Не удалось открыть .xlsx: {e}") from e
    blocks: list[Block] = []
    for ws in wb.worksheets:
        blocks.append(Block(f"Лист «{ws.title}»", heading_level=1))
        header: list[str] | None = None
        for r_i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            cells = ["" if v is None else " ".join(str(v).split()) for v in row]
            if not any(cells):
                continue
            if header is None:
                header = cells
                continue
            pairs = [f"{h or f'Колонка {i + 1}'}: {v}" for i, (h, v) in enumerate(zip(header, cells)) if v]
            first = cells[0]
            if re.fullmatch(r"\d{1,2}(\.\d{1,3})+\.?", first):
                # первая колонка — номер пункта: строка становится обычным пунктом «4.1 ...»
                rest = [f"{h or f'Колонка {i + 2}'}: {v}" for i, (h, v) in enumerate(zip(header[1:], cells[1:])) if v]
                blocks.append(Block(f"{first.rstrip('.')} " + "; ".join(rest)))
            else:
                blocks.append(Block("; ".join(pairs), is_table_row=True, source=f"лист «{ws.title}», строка {r_i}"))
    wb.close()
    return blocks


_HTML_BLOCKS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre", "dt", "dd", "blockquote"]


def parse_html(path: Path) -> list[Block]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:  # pragma: no cover
        raise ParseError("Не установлен beautifulsoup4") from e
    soup = BeautifulSoup(read_text_file(path), "lxml")
    for tag in soup(["script", "style", "nav", "noscript", "header", "footer"]):
        tag.decompose()
    blocks: list[Block] = []
    table_no = 0
    seen_tables: dict[int, int] = {}
    for el in soup.find_all(_HTML_BLOCKS):
        if any(a.name in _HTML_BLOCKS for a in el.parents):
            continue
        text = " ".join(el.get_text(" ").split())
        if not text:
            continue
        if el.name.startswith("h") and len(el.name) == 2:
            blocks.append(Block(text, heading_level=int(el.name[1])))
        elif el.name == "li":
            parent = el.find_parent(["ol", "ul"])
            if parent is not None and parent.name == "ol":
                idx = 1 + sum(1 for s in el.find_previous_siblings("li"))
                start = int(parent.get("start", 1) or 1)
                text = f"{idx + start - 1}. {text}"
            blocks.append(Block(text))
        elif el.name == "tr":
            table = el.find_parent("table")
            key = id(table)
            if key not in seen_tables:
                table_no += 1
                seen_tables[key] = table_no
            cells = [" ".join(c.get_text(" ").split()) for c in el.find_all(["td", "th"])]
            if el.find("th") and not el.find("td"):
                continue  # строка заголовка
            blocks.append(Block(" | ".join(c for c in cells if c), is_table_row=True, source=f"табл. {seen_tables[key]}"))
        else:
            blocks.append(Block(text))
    return blocks


def convert_doc(path: Path, soffice_cmd: str) -> tuple[Path, tempfile.TemporaryDirectory]:
    exe = shutil.which(soffice_cmd) or (soffice_cmd if Path(soffice_cmd).exists() else None)
    if not exe:
        raise ParseError(
            "Формат .doc читается только через LibreOffice, а он не установлен. "
            "Сохраните файл как .docx или укажите путь к soffice в SOFFICE_CMD."
        )
    tmp = tempfile.TemporaryDirectory(prefix="doc2docx_")
    try:
        subprocess.run(
            [exe, "--headless", "--norestore", "-env:UserInstallation=" + (Path(tmp.name) / "profile").as_uri(),
             "--convert-to", "docx", "--outdir", tmp.name, str(path)],
            check=True, capture_output=True, timeout=180,
        )
    except (subprocess.SubprocessError, OSError) as e:
        tmp.cleanup()
        raise ParseError(f"LibreOffice не смог сконвертировать .doc: {e}") from e
    out = Path(tmp.name) / (path.stem + ".docx")
    if not out.exists():
        tmp.cleanup()
        raise ParseError("LibreOffice не создал .docx из .doc")
    return out, tmp
