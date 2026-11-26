"""Разбор .docx с восстановлением автонумерации Word (w:numPr)."""
from __future__ import annotations

import re
from pathlib import Path

from .model import Block, ParseError

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _val(el, tag: str) -> str | None:
    if el is None:
        return None
    child = el.find(f"{W}{tag}")
    return None if child is None else child.get(f"{W}val")


class _Numbering:
    """Счётчики нумерации по numbering.xml."""

    def __init__(self, doc) -> None:
        self.levels: dict[str, dict[int, dict]] = {}  # abstractNumId -> ilvl -> {fmt, text, start}
        self.num_to_abs: dict[str, str] = {}
        self.overrides: dict[str, dict[int, int]] = {}
        self.counters: dict[str, list[int]] = {}
        try:
            root = doc.part.numbering_part.element
        except (KeyError, NotImplementedError, AttributeError):
            return
        for an in root.findall(f"{W}abstractNum"):
            aid = an.get(f"{W}abstractNumId")
            lv = {}
            for lvl in an.findall(f"{W}lvl"):
                ilvl = int(lvl.get(f"{W}ilvl", "0"))
                lv[ilvl] = {
                    "fmt": _val(lvl, "numFmt") or "decimal",
                    "text": _val(lvl, "lvlText") or "",
                    "start": int(_val(lvl, "start") or 1),
                }
            self.levels[aid] = lv
        for num in root.findall(f"{W}num"):
            nid = num.get(f"{W}numId")
            self.num_to_abs[nid] = _val(num, "abstractNumId") or ""
            for ov in num.findall(f"{W}lvlOverride"):
                so = _val(ov, "startOverride")
                if so is not None:
                    self.overrides.setdefault(nid, {})[int(ov.get(f"{W}ilvl", "0"))] = int(so)

    def label(self, num_id: str, ilvl: int) -> str | None:
        aid = self.num_to_abs.get(num_id)
        if aid is None or aid not in self.levels:
            return None
        levels = self.levels[aid]
        lvl = levels.get(ilvl)
        if lvl is None or lvl["fmt"] in ("bullet", "none"):
            return None
        # общие счётчики на абстрактный список: так Word продолжает нумерацию разных numId одного стиля
        cnt = self.counters.setdefault(aid, [0] * 9)
        if cnt[ilvl] == 0:
            cnt[ilvl] = self.overrides.get(num_id, {}).get(ilvl, lvl["start"])
        else:
            cnt[ilvl] += 1
        for deeper in range(ilvl + 1, 9):
            cnt[deeper] = 0

        def repl(m: re.Match) -> str:
            k = int(m.group(1)) - 1
            info = levels.get(k, {"fmt": "decimal", "start": 1})
            value = cnt[k] or info.get("start", 1)
            return _fmt(value, info.get("fmt", "decimal"))

        text = re.sub(r"%(\d)", repl, lvl["text"]).strip()
        return text or None


def _fmt(n: int, fmt: str) -> str:
    if fmt in ("lowerLetter", "upperLetter"):
        s = "abcdefghijklmnopqrstuvwxyz"[(n - 1) % 26]
        return s.upper() if fmt == "upperLetter" else s
    if fmt == "russianLower":
        return "абвгдежзиклмнопрстуфхцчшщэюя"[(n - 1) % 28]
    if fmt == "russianUpper":
        return "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ"[(n - 1) % 28]
    if fmt in ("lowerRoman", "upperRoman"):
        vals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
        out, x = "", n
        for v, r in vals:
            while x >= v:
                out, x = out + r, x - v
        return out.upper() if fmt == "upperRoman" else out
    return str(n)


def _style_chain(style):
    while style is not None:
        yield style
        style = style.base_style


def _num_pr(paragraph) -> tuple[str, int] | None:
    ppr = paragraph._p.pPr
    if ppr is not None and ppr.numPr is not None:
        nid = ppr.numPr.numId.val if ppr.numPr.numId is not None else None
        ilvl = ppr.numPr.ilvl.val if ppr.numPr.ilvl is not None else None
        if nid is not None:
            if str(nid) == "0":
                return None
            return str(nid), int(ilvl or 0)
    for st in _style_chain(paragraph.style):
        sppr = st.element.pPr
        if sppr is not None and sppr.numPr is not None and sppr.numPr.numId is not None:
            ilvl = sppr.numPr.ilvl.val if sppr.numPr.ilvl is not None else 0
            return str(sppr.numPr.numId.val), int(ilvl or 0)
    return None


def _heading_level(paragraph) -> int:
    for st in _style_chain(paragraph.style):
        name = (st.name or "").lower()
        m = re.match(r"(heading|заголовок)\s*(\d)", name)
        if m:
            return int(m.group(2))
        if name in ("title", "название"):
            return 1
    ol = paragraph._p.pPr.find(f"{W}outlineLvl") if paragraph._p.pPr is not None else None
    if ol is not None:
        return int(ol.get(f"{W}val", "0")) + 1
    return 0


def parse_docx(path: Path) -> list[Block]:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as e:  # pragma: no cover
        raise ParseError("Не установлен python-docx") from e
    try:
        document = docx.Document(str(path))
    except Exception as e:
        raise ParseError(f"Не удалось открыть .docx: {e}") from e

    numbering = _Numbering(document)
    blocks: list[Block] = []
    page = 1
    table_no = 0
    for child in document.element.body.iterchildren():
        if child.tag == f"{W}p":
            p = Paragraph(child, document)
            breaks = len(child.findall(f".//{W}lastRenderedPageBreak")) + sum(
                1 for br in child.findall(f".//{W}br") if br.get(f"{W}type") == "page"
            )
            text = p.text.strip()
            if text:
                level = _heading_level(p)
                npr = _num_pr(p)
                label = numbering.label(*npr) if npr else None
                if label and not text.startswith(label):
                    # «1.» + «Общие положения»; маркеры вида «а)» тоже сохраняем
                    text = f"{label} {text}"
                blocks.append(Block(text, page=page, heading_level=level))
            page += breaks
        elif child.tag == f"{W}tbl":
            table_no += 1
            table = Table(child, document)
            rows = table.rows
            header: list[str] = []
            for r_i, row in enumerate(rows):
                cells = []
                seen = set()
                for c in row.cells:  # объединённые ячейки повторяются — убираем дубли
                    if id(c._tc) in seen:
                        continue
                    seen.add(id(c._tc))
                    cells.append(" ".join(c.text.split()))
                if not any(cells):
                    continue
                if r_i == 0:
                    header = cells
                    continue
                if header and len(header) == len(cells):
                    text = "; ".join(f"{h}: {v}" for h, v in zip(header, cells) if v)
                else:
                    text = " | ".join(x for x in cells if x)
                blocks.append(Block(text, page=page, is_table_row=True, source=f"табл. {table_no}, строка {r_i}"))
    if page == 1:
        # в файле нет разрывов страниц (например, он создан не в Word) — номер страницы неизвестен
        for b in blocks:
            b.page = None
    return blocks
