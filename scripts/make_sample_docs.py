"""Создать условные регламенты во всех поддерживаемых форматах.

    python scripts/make_sample_docs.py data/sample_docs
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sample_content import DOCS  # noqa: E402

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial.ttf",
]


def font_path() -> str:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("Нужен TTF-шрифт с кириллицей (DejaVuSans или Arial)")


def title_of(doc: dict) -> str:
    return doc.get("title_line") or Path(doc["file"]).name.replace("_", " ")


def plain_lines(doc: dict, numbered_sections: bool = True) -> list[str]:
    lines = [title_of(doc), ""]
    for i, (sec, clauses) in enumerate(doc["sections"], start=1):
        sec_no = clauses[0][0].split(".")[0]
        lines.append(f"{sec_no}. {sec}" if numbered_sections else sec)
        for no, text in clauses:
            lines.append(f"{no}. {text}")
        lines.append("")
    return lines


# --- docx ----------------------------------------------------------------------------------
NUMBERING_XML = (
    '<w:abstractNum xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" w:abstractNumId="90">'
    '<w:multiLevelType w:val="multilevel"/>'
    '<w:lvl w:ilvl="0"><w:start w:val="{start}"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>'
    '</w:abstractNum>'
)


def write_docx(doc: dict, path: Path, auto: bool) -> None:
    import docx
    from docx.oxml import parse_xml
    from docx.oxml.ns import qn

    d = docx.Document()
    d.add_heading(title_of(doc), level=0)
    num_id = None
    if auto:
        numbering = d.part.numbering_part.element
        first_sec = int(doc["sections"][0][1][0][0].split(".")[0])
        absnum = parse_xml(NUMBERING_XML.replace("{start}", str(first_sec)))
        numbering.insert(0, absnum)
        num = parse_xml('<w:num xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" w:numId="90">'
                        '<w:abstractNumId w:val="90"/></w:num>')
        numbering.append(num)
        num_id = "90"

    def numbered(p, ilvl: int) -> None:
        ppr = p._p.get_or_add_pPr()
        numpr = parse_xml('<w:numPr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                          f'<w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/></w:numPr>')
        ppr.insert(0, numpr) if ppr.find(qn("w:pStyle")) is None else ppr.find(qn("w:pStyle")).addnext(numpr)

    for sec, clauses in doc["sections"]:
        sec_no = clauses[0][0].split(".")[0]
        if auto:
            h = d.add_heading(sec, level=1)
            numbered(h, 0)
        else:
            d.add_heading(f"{sec_no}. {sec}", level=1)
        for no, text in clauses:
            if auto:
                numbered(d.add_paragraph(text), 1)
            else:
                d.add_paragraph(f"{no}. {text}")
    d.save(str(path))


# --- pdf -----------------------------------------------------------------------------------
def write_pdf(doc: dict, path: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    pdfmetrics.registerFont(TTFont("Cyr", font_path()))
    st = ParagraphStyle("n", fontName="Cyr", fontSize=11, leading=15)
    h = ParagraphStyle("h", fontName="Cyr", fontSize=13, leading=18, spaceBefore=8)
    story = []
    for line in plain_lines(doc):
        if not line:
            story.append(Spacer(1, 6))
            continue
        is_head = line == title_of(doc) or (line.split(".")[0].isdigit() and line.count(".") == 1 and len(line) < 80)
        story.append(Paragraph(line, h if is_head else st))
    SimpleDocTemplate(str(path), pagesize=A4).build(story)


def render_image(lines: list[str]):
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(font_path(), 30)
    width, margin = 2000, 100
    wrapped: list[str] = []
    for line in lines:
        words, cur = line.split(), ""
        for w in words:
            test = f"{cur} {w}".strip()
            if font.getlength(test) > width - 2 * margin and cur:
                wrapped.append(cur)
                cur = w
            else:
                cur = test
        wrapped.append(cur)
    height = margin * 2 + 46 * len(wrapped)
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(wrapped):
        draw.text((margin, margin + i * 46), line, fill="black", font=font)
    return img


def write_scan_pdf(doc: dict, path: Path) -> None:
    img = render_image(plain_lines(doc))
    img.save(str(path), "PDF", resolution=200.0)


def write_png(doc: dict, path: Path) -> None:
    render_image(plain_lines(doc)).save(str(path))


# --- остальные форматы -------------------------------------------------------------------------
def write_txt(doc: dict, path: Path) -> None:
    path.write_text("\n".join(plain_lines(doc)), encoding="utf-8")


def write_md(doc: dict, path: Path) -> None:
    out = [f"# {title_of(doc)}", ""]
    for sec, clauses in doc["sections"]:
        out += [f"## {sec}", ""] + [f"{no}. {text}" for no, text in clauses] + [""]
    path.write_text("\n".join(out), encoding="utf-8")


def write_rtf(doc: dict, path: Path) -> None:
    def esc(s: str) -> str:
        return "".join(c if ord(c) < 128 else f"\\u{ord(c) if ord(c) < 32768 else ord(c) - 65536}?" for c in s)

    body = "".join(f"{{\\pard {esc(line)}\\par}}\n" for line in plain_lines(doc) if line)
    path.write_text("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\n" + body + "}", encoding="ascii")


def write_html(doc: dict, path: Path) -> None:
    import html

    parts = [f"<html><head><meta charset='utf-8'><title>{html.escape(title_of(doc))}</title></head><body>",
             "<nav>Меню портала</nav>", f"<h1>{html.escape(title_of(doc))}</h1>"]
    for sec, clauses in doc["sections"]:
        sec_no = clauses[0][0].split(".")[0]
        parts.append(f"<h2>{sec_no}. {html.escape(sec)}</h2>")
        parts += [f"<p>{no}. {html.escape(text)}</p>" for no, text in clauses]
    parts.append("<script>console.log('x')</script></body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_odt(doc: dict, path: Path) -> None:
    from odf.opendocument import OpenDocumentText
    from odf.text import H, P

    d = OpenDocumentText()
    d.text.addElement(H(outlinelevel=1, text=title_of(doc)))
    for sec, clauses in doc["sections"]:
        sec_no = clauses[0][0].split(".")[0]
        d.text.addElement(H(outlinelevel=2, text=f"{sec_no}. {sec}"))
        for no, text in clauses:
            d.text.addElement(P(text=f"{no}. {text}"))
    d.save(str(path))


def write_xlsx(doc: dict, path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = doc["sections"][0][0]
    ws.append(["Пункт", "Правило"])
    for sec, clauses in doc["sections"]:
        for no, text in clauses:
            ws.append([no, text])
    wb.save(str(path))


def write_doc(doc: dict, path: Path) -> bool:
    exe = shutil.which("soffice")
    if not exe:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / (path.stem + ".docx")
        write_docx(doc, src, auto=False)
        subprocess.run([exe, "--headless", "-env:UserInstallation=" + (Path(tmp) / "p").as_uri(),
                        "--convert-to", "doc", "--outdir", tmp, str(src)], check=True, capture_output=True, timeout=120)
        shutil.move(str(Path(tmp) / (path.stem + ".doc")), path)
    return True


WRITERS = {
    "docx_auto": (".docx", lambda d, p: write_docx(d, p, True)),
    "docx_manual": (".docx", lambda d, p: write_docx(d, p, False)),
    "pdf": (".pdf", write_pdf),
    "pdf_scan": (".pdf", write_scan_pdf),
    "png": (".png", write_png),
    "txt": (".txt", write_txt),
    "md": (".md", write_md),
    "rtf": (".rtf", write_rtf),
    "html": (".html", write_html),
    "odt": (".odt", write_odt),
    "xlsx": (".xlsx", write_xlsx),
}


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for doc in DOCS:
        rel = Path(doc["file"])
        (out / rel.parent).mkdir(parents=True, exist_ok=True)
        if doc["fmt"] == "doc":
            target = out / rel.with_suffix(".doc")
            if not write_doc(doc, target):
                target = out / rel.with_suffix(".docx")
                write_docx(doc, target, auto=False)
                print(f"LibreOffice не найден, {rel} сохранён как .docx")
            print(target)
            continue
        ext, writer = WRITERS[doc["fmt"]]
        target = out / rel.with_suffix(ext)
        writer(doc, target)
        print(target)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/sample_docs"))
