"""PDF: текстовый слой через pypdf (запасной вариант — pdfplumber), сканы через pypdfium2 + Tesseract."""
from __future__ import annotations

import logging
from pathlib import Path

from .model import Block, ParseError
from .ocr import ocr_image

log = logging.getLogger(__name__)
logging.getLogger("pypdf").setLevel(logging.ERROR)
logging.getLogger("pdfminer").setLevel(logging.ERROR)


def _lines(text: str, page: int) -> list[Block]:
    return [Block(line, page=page) for line in text.splitlines() if line.strip()]


def _text_pages(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:
                raise ParseError("PDF защищён паролем") from e
        return [(p.extract_text() or "") for p in reader.pages]
    except ParseError:
        raise
    except Exception as e:
        log.warning("pypdf не прочитал %s (%s), пробую pdfplumber", path.name, e)
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return [(p.extract_text() or "") for p in pdf.pages]
    except Exception as e:
        raise ParseError(f"Не удалось прочитать PDF: {e}") from e


def parse_pdf(path: Path, settings) -> list[Block]:
    pages = _text_pages(path)
    blocks: list[Block] = []
    ocr_pages: list[int] = []
    for i, text in enumerate(pages, start=1):
        if len(text.strip()) >= settings.ocr_min_chars_per_page:
            blocks.extend(_lines(text, i))
        else:
            ocr_pages.append(i)
    if ocr_pages:
        try:
            import pypdfium2 as pdfium
        except ImportError as e:  # pragma: no cover
            raise ParseError("Не установлен pypdfium2 для распознавания сканов") from e
        pdf = pdfium.PdfDocument(str(path))
        try:
            ocr_blocks: dict[int, list[Block]] = {}
            for i in ocr_pages:
                image = pdf[i - 1].render(scale=settings.ocr_dpi / 72).to_pil()
                ocr_blocks[i] = _lines(ocr_image(image, settings), i)
        finally:
            pdf.close()
        merged: list[Block] = []
        by_page: dict[int, list[Block]] = {}
        for b in blocks:
            by_page.setdefault(b.page or 0, []).append(b)
        by_page.update(ocr_blocks)
        for p in sorted(by_page):
            merged.extend(by_page[p])
        blocks = merged
    return blocks


def parse_image(path: Path, settings) -> list[Block]:
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.load()
            text = ocr_image(img, settings)
    except ParseError:
        raise
    except Exception as e:
        raise ParseError(f"Не удалось открыть картинку: {e}") from e
    return _lines(text, 1)
