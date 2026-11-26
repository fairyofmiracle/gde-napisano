"""Нормализация текста и простая токенизация для BM25 и правил."""
from __future__ import annotations

import re
import unicodedata

_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_STOP = set(
    "и в во на с со к ко по о об от до из за для не ни а но или ли же бы что как это так то "
    "у при про над под без между если когда где кто чем мне мой моя меня я мы вы он она они их "
    "его ее её ли можно нужно ли какой какая какие каких сколько есть быть был была".split()
)
_ENDINGS = sorted(
    "иями ями ами ией иям иях ого ему ому ыми ими его ая яя ое ее ые ие ый ий ой ей ую юю ов ев "
    "ам ям ах ях ом ем ах ию ия ие ии ость ости остью а я о е ы и у ю ь".split(),
    key=len,
    reverse=True,
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    text = re.sub(r"[«»“”„\"'`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def stem(word: str) -> str:
    if word.isdigit() or len(word) <= 4:
        return word
    for e in _ENDINGS:
        if word.endswith(e) and len(word) - len(e) >= 4:
            return word[: -len(e)]
    return word


def tokens(text: str) -> list[str]:
    return [stem(w) for w in _WORD_RE.findall(normalize(text)) if w not in _STOP]
