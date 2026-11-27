"""Детерминированная модель для тестов: отвечает дословной цитатой первого пункта.
На запрос переформулировки возвращает вопрос без вежливых слов (как правило-заготовка)."""
from __future__ import annotations

import json
import re

from .base import LLM, LLMResult
from .prompts import REWRITE_SYSTEM, SIMPLIFY_SYSTEM


class FakeLLM(LLM):
    provider = "fake"
    model = "fake"

    def __init__(self, mode: str = "quote") -> None:
        self.mode = mode  # quote | wrong_quote | unanswerable | wrong_number | advice_number
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        self.calls.append((system, user))
        if system == REWRITE_SYSTEM:
            from app.rewrite import rule_rewrite

            q = user.split("\n", 1)[-1].strip()
            text = json.dumps({"query": rule_rewrite(q), "subquestions": []}, ensure_ascii=False)
            return self.log_usage(LLMResult(text, len(system + user) // 4, len(text) // 4, self.provider, self.model))
        if system == SIMPLIFY_SYSTEM:
            m = re.search(r"Исходный ответ[^\n]*:\n(.+?)(?:\nЦитаты|\nГлоссарий|$)", user, re.S)
            src = (m.group(1).strip() if m else "Ответ по пункту.")
            glossary = re.findall(r"^(\S+) — (.+)$", user, re.M)
            text_out = src
            for abbr, full in glossary:
                if abbr and full and abbr not in ("Исходный", "Вопрос", "Цитаты"):
                    text_out = re.sub(rf"(?<!\w){re.escape(abbr)}(?!\w)", f"{abbr} ({full})", text_out, count=1)
            text = json.dumps({"text": text_out}, ensure_ascii=False)
            return self.log_usage(LLMResult(text, len(system + user) // 4, len(text) // 4, self.provider, self.model))
        m = re.search(r"<<<ПУНКТ 1>>>\n(.+?)\n<<<КОНЕЦ ПУНКТА 1>>>", user, re.S)
        source = m.group(1).strip() if m else ""
        first = re.split(r"(?<=[.!?])\s", source, maxsplit=1)[0]
        if self.mode == "unanswerable" or not source:
            data = {"answerable": False, "answer": "", "used": [], "quotes": [], "advice": ""}
        else:
            quote = "Этого текста в документе нет совсем." if self.mode == "wrong_quote" else first
            answer = f"{first} [1]"
            if self.mode == "wrong_number":
                answer = f"{first} Срок — 999 дней. [1]"
            advice = "Подайте заявление за 777 дней." if self.mode == "advice_number" else "Действуйте по пункту [1]."
            data = {"answerable": True, "answer": answer, "used": [1],
                    "quotes": [{"n": 1, "quote": quote}], "advice": advice}
        text = json.dumps(data, ensure_ascii=False)
        return self.log_usage(LLMResult(text, len(system + user) // 4, len(text) // 4, self.provider, self.model))
