"""Кнопка «Не понял»: пересказ того же ответа без нового поиска и без новых чисел."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.answer import unsupported_numbers
from app.llm.base import LLM, LLMResult
from app.llm.fake import FakeLLM
from app.llm.prompts import SIMPLIFY_SYSTEM
from app.main import create_app
from app.simplify import can_simplify, expand_abbreviations, payload_from_answer, simplify
from tests.test_answers import _load

Q_OK = "Сколько календарных дней ежегодного оплачиваемого отпуска?"


class NumberInventingLLM(LLM):
    provider = "stub"
    model = "stub"

    def chat(self, system, user, json_mode=False):
        if system == SIMPLIFY_SYSTEM:
            text = json.dumps({"text": "Срок ответа — 999 дней [1]."}, ensure_ascii=False)
            return LLMResult(text, 10, 5, self.provider, self.model)
        return FakeLLM().chat(system, user, json_mode)


class SimpleLLM(LLM):
    provider = "stub"
    model = "stub"

    def chat(self, system, user, json_mode=False):
        if system == SIMPLIFY_SYSTEM:
            text = json.dumps({
                "text": "Ежегодный оплачиваемый отпуск длится 28 календарных дней [1].",
            }, ensure_ascii=False)
            return LLMResult(text, 10, 8, self.provider, self.model)
        return FakeLLM().chat(system, user, json_mode)


def _cite():
    return {"n": 1, "point_id": "p1", "doc_id": "d1", "doc_title": "Положение об отпусках",
            "clause_no": "3.1", "ref": "Положение об отпусках, п. 3.1",
            "quote": "отпуска составляет 28 календарных дней", "verified": True}


def test_can_simplify_only_for_regulation_answers():
    cite = _cite()
    ok = {"mode": "confident", "text": "Отпуск 28 дней [1].", "citations": [cite]}
    assert can_simplify(ok)
    assert not can_simplify(dict(ok, escalation={"id": 1}))
    assert not can_simplify(dict(ok, escalated=True))
    assert not can_simplify(dict(ok, answer_kind="quote_only"))
    assert not can_simplify({"mode": "not_found", "text": "нет", "citations": []})
    assert not can_simplify({"mode": "uncertain", "text": "варианты", "citations": [cite]})
    assert not can_simplify({"mode": "faq", "text": "разъяснение", "citations": []})
    assert not can_simplify({"mode": "confident", "text": "Отпуск 28 дней [1].", "citations": []})


def test_expand_abbreviations_and_number_check():
    text = expand_abbreviations("ПА принимает заявку за 4 часа.", [{"abbr": "ПА", "full": "пресейл-архитектор"}])
    assert "ПА (пресейл-архитектор)" in text
    # повторно не дублируем
    assert expand_abbreviations(text, [{"abbr": "ПА", "full": "пресейл-архитектор"}]).count("пресейл-архитектор") == 1
    already = "Пресейл-архитектор (ПА) должен взять заявку."
    assert expand_abbreviations(already, [{"abbr": "ПА", "full": "Пресейл-архитектор"}]) == already
    src = [SimpleNamespace(doc_title="Регламент", clause_no="5.1", text="принять заявку за 4 рабочих часа")]
    assert unsupported_numbers("за 4 рабочих часа [1]", src) == []
    assert "999" in unsupported_numbers("за 999 дней [1]", src)


def test_simplify_keeps_citations_and_rejects_new_numbers():
    payload = {
        "question": "Сколько дней отпуска?",
        "mode": "confident",
        "text": "Ежегодный отпуск составляет 28 календарных дней [1].",
        "citations": [_cite()],
        "glossary": [],
        "advice": "",
    }
    good = simplify(SimpleLLM(), payload)
    assert good["source"] == "model"
    assert good["simplified"] is True
    assert "28 календарных дней" in good["text"]
    assert "[1]" in good["text"]
    assert good["citations"] == payload["citations"]
    assert unsupported_numbers(good["text"], [type("C", (), {
        "doc_title": "Положение об отпусках", "clause_no": "3.1",
        "text": payload["text"] + " " + payload["citations"][0]["quote"]})()]) == []

    bad = simplify(NumberInventingLLM(), payload)
    assert bad["source"] == "fallback"
    assert "999" not in bad["text"]
    assert bad["text"] == payload["text"]
    assert bad["citations"] == payload["citations"]


def test_api_simplify_by_answer_id_and_rejects_escalation(services_factory):
    llm = SimpleLLM()
    svc = services_factory(llm=llm)
    _load(svc)
    with TestClient(create_app(services=svc)) as c:
        body = c.post("/api/chat", json={"question": Q_OK}).json()
        assert body["mode"] == "confident"
        assert body["can_simplify"] is True
        aid = body["answer_id"]
        r = c.post("/api/simplify", json={"answer_id": aid})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["answer_id"] == aid
        assert data["citations"]
        assert data["citations"][0]["clause_no"] == body["citations"][0]["clause_no"]
        assert data["citations"][0]["quote"] == body["citations"][0]["quote"]
        assert "28" in data["text"]
        # payload only, without id
        r2 = c.post("/api/simplify", json={
            "question": body["question"], "text": body["text"], "citations": body["citations"],
            "glossary": body["glossary"], "mode": "confident",
        })
        assert r2.status_code == 200
        # not_found / offtopic
        refuse = c.post("/api/chat", json={"question": "привет"}).json()
        assert refuse["can_simplify"] is False
        bad = c.post("/api/simplify", json={"answer_id": refuse["answer_id"]})
        assert bad.status_code == 400
        missing = c.post("/api/simplify", json={"answer_id": "missing-answer-id-xx"})
        assert missing.status_code == 404


def test_payload_stored_without_email(services_factory):
    svc = services_factory()
    _load(svc)
    out = svc.assistant.ask(Q_OK, session_id="s1", user_email="person@example.local", user_name="Тест")
    row = svc.memory.get_answer(out["answer_id"])
    raw = row["payload"]
    assert "person@example.local" not in raw
    data = json.loads(raw)
    assert data["citations"] and data["text"]
    assert payload_from_answer(out)["question"] == Q_OK
