"""Глоссарий сокращений: расширение поискового запроса и расшифровка терминов в ответе.

Файл TSV: «сокращение<TAB>расшифровка». В одной строке может быть несколько сокращений через запятую
(«КСУП, ИСУП»). Расшифровки берутся только из файла, никаких внешних знаний.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.text import tokens

log = logging.getLogger(__name__)
_LETTER = r"0-9A-Za-zА-Яа-яЁё"


@dataclass
class Term:
    abbr: str
    full: str
    pattern: re.Pattern
    full_stems: frozenset

    def as_dict(self) -> dict:
        return {"abbr": self.abbr, "full": self.full}


def _uppercase_count(s: str) -> int:
    return sum(1 for ch in s if ch.isupper())


class Glossary:
    def __init__(self, terms: list[Term]) -> None:
        self.terms = terms

    @classmethod
    def load(cls, path: Path | None) -> Glossary:
        terms: list[Term] = []
        if not path or not Path(path).exists():
            if path:
                log.info("Глоссарий %s не найден — работаю без расширения запроса", path)
            return cls(terms)
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if "\t" not in line or line.lstrip().startswith("#"):
                continue
            left, full = (x.strip() for x in line.split("\t", 1))
            if not left or not full:
                continue
            for abbr in (a.strip() for a in left.split(",")):
                if not abbr:
                    continue
                pat = re.compile(rf"(?<![{_LETTER}]){re.escape(abbr)}(?![{_LETTER}])", re.I)
                terms.append(Term(abbr, full, pat, frozenset(tokens(full))))
        return cls(terms)

    def __len__(self) -> int:
        return len(self.terms)

    def find(self, text: str) -> list[Term]:
        """Сокращения, которые встречаются в тексте. Регистр не важен для «ЗнП», но «по» не равно «ПО»:
        совпадение засчитывается, если в найденном слове не меньше двух заглавных букв или оно совпадает точно."""
        out, seen = [], set()
        for t in self.terms:
            for m in t.pattern.finditer(text or ""):
                word = m.group(0)
                if word == t.abbr or _uppercase_count(word) >= min(2, _uppercase_count(t.abbr)):
                    if t.abbr not in seen:
                        seen.add(t.abbr)
                        out.append(t)
                    break
        return out

    def find_full(self, text: str) -> list[Term]:
        """Термины, записанные полностью («руководитель проекта» → РП)."""
        have = set(tokens(text or ""))
        return [t for t in self.terms if len(t.full_stems) >= 2 and t.full_stems <= have]

    def expand(self, question: str) -> tuple[str, list[Term]]:
        """Запрос для поиска: вопрос + расшифровки найденных сокращений + сокращения для полных терминов."""
        short = self.find(question)
        full = [t for t in self.find_full(question) if t not in short]
        extra = [t.full for t in short] + [t.abbr for t in full]
        if not extra:
            return question, short
        return f"{question} ({'; '.join(extra)})", short

    def explain(self, *texts: str, limit: int = 8) -> list[dict]:
        out, seen = [], set()
        for text in texts:
            for t in self.find(text):
                if t.abbr not in seen:
                    seen.add(t.abbr)
                    out.append(t.as_dict())
        return out[:limit]
