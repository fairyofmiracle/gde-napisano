"""Распознавание текста (Tesseract) для сканов и картинок."""
from __future__ import annotations

import shutil

from .model import ParseError


def _tesseract(settings):
    try:
        import pytesseract
    except ImportError as e:  # pragma: no cover
        raise ParseError("Не установлен pytesseract") from e
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    elif not shutil.which("tesseract"):
        raise ParseError(
            "Для сканов и картинок нужен Tesseract с русским языком (tesseract-ocr-rus). "
            "Он есть в Docker-образе; без Docker укажите путь в TESSERACT_CMD."
        )
    return pytesseract


def ocr_image(image, settings) -> str:
    pytesseract = _tesseract(settings)
    img = image.convert("L")
    if img.width < 1200:  # мелкие картинки распознаются хуже — увеличиваем
        k = 1200 / max(img.width, 1)
        img = img.resize((int(img.width * k), int(img.height * k)))
    try:
        return pytesseract.image_to_string(img, lang=settings.ocr_lang, config="--psm 4")
    except pytesseract.TesseractError as e:
        raise ParseError(f"Ошибка распознавания: {e}. Проверьте, что установлен язык rus.") from e
