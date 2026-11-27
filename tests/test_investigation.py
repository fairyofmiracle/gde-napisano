"""Ход работы как расследование: улики → сверка → вердикт, только из данных конвейера."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import investigation
from app.main import create_app
from app.schemas import ChatOut, InvestigationOut
from app.search import Candidate
from bot.formatting import format_answer, investigation_brief
from tests.test_answers import _load

S = SimpleNamespace(confident_threshold=0.5, uncertain_threshold=0.03)
Q_OK = "Сколько календарных дней ежегодного оплачиваемого отпуска?"


def _cand(pid, clause, score, doc="d1", title="Пресейловая деятельность Дирекции ИБ"):
    c = Candidate(pid, doc, title, "dib", clause, "", 3, "clause", "текст")
    c.score = score
    return c


def _cite(n, pid, clause, doc="d1"):
    return {"n": n, "point_id": pid, "doc_id": doc, "doc_title": "Пресейловая деятельность Дирекции ИБ",
            "clause_no": clause, "ref": f"Пресейловая деятельность Дирекции ИБ, п. {clause}", "quote": "q",
            "verified": True, "link": f"/doc/{doc}?p={pid}"}


STEPS_OK = [
    {"step": "guard", "title": "Проверка вопроса", "status": "ok", "detail": "Фильтр лексики пройден", "ms": None},
    {"step": "rerank", "title": "Реранжирование", "status": "ok", "detail": "Проверено пунктов: 8", "ms": 9000},
    {"step": "assess", "title": "Оценка уверенности", "status": "ok", "detail": "Оценка 0.89 ≥ порога 0.50", "ms": None},
    {"step": "generate", "title": "Ответ модели", "status": "ok", "detail": "gigachat", "ms": 1700},
    {"step": "verify_quote", "title": "Проверка цитаты", "status": "ok", "detail": "Подтверждено цитат: 2 из 2"},
    {"step": "verify_numbers", "title": "Проверка чисел и дат", "status": "ok", "detail": "Чисел в ответе нет"},
    {"step": "conflict", "title": "Проверка противоречий", "status": "ok", "detail": "Не противоречат"},
    {"step": "decide", "title": "Решение", "status": "ok", "detail": "Ответ с проверенной цитатой"},
]


def test_answer_has_three_stages_with_links_and_high_confidence():
    cands = [_cand("p1", "4.2.2", 0.89), _cand("p2", "3.2.1", 0.7), _cand("p3", "9.9", 0.2)]
    out = {"mode": "confident", "answer_kind": "answer", "score": 0.89, "steps": STEPS_OK, "warnings": [],
           "citations": [_cite(1, "p1", "4.2.2"), _cite(2, "p2", "3.2.1")]}
    inv = investigation.build(out, cands, S)
    InvestigationOut.model_validate(inv)
    ev = inv["evidence"]["items"]
    assert [i["role"] for i in ev] == ["cited", "cited", "candidate"]
    assert ev[0]["link"] == "/doc/d1?p=p1" and ev[2]["link"] == "/doc/d1?p=p3"
    assert "в ответе: 2" in inv["evidence"]["summary"]
    labels = [i["label"] for i in inv["check"]["items"]]
    assert labels == ["Порог уверенности", "Цитата", "Числа и даты", "Противоречия", "Согласуются"]
    assert "rerank" not in str(labels) and "Тема вопроса" not in labels  # ок-фильтр лексики не шумит
    assert inv["check"]["summary"].startswith("Всё сходится")
    v = inv["verdict"]
    assert v["kind"] == "answer" and v["confidence"] == "high" and v["confidence_label"] == "высокая"
    assert "0.89" in v["why"] and "0.50" in v["why"]


def test_conflict_verdict_names_both_clauses_and_ticket():
    cands = [_cand("p1", "5.2.2", 0.9), _cand("p2", "5.2.3", 0.8)]
    steps = STEPS_OK[:-2] + [{"step": "conflict", "title": "Проверка противоречий", "status": "warn",
                              "detail": "Найдено расхождение внутри документа: п. 5.2.2 и п. 5.2.3"}]
    out = {"mode": "confident", "answer_kind": "answer", "score": 0.9, "steps": steps, "conflict": True,
           "answer_check": "internal_conflict", "warnings": [], "escalation": {"id": 7},
           "citations": [_cite(1, "p1", "5.2.2"), _cite(2, "p2", "5.2.3")]}
    inv = investigation.build(out, cands, S)
    assert [i["role"] for i in inv["evidence"]["items"]] == ["conflict", "conflict"]
    rasx = next(i for i in inv["check"]["items"] if i["label"] == "Расходятся")
    assert not any(i["label"] == "Противоречия" for i in inv["check"]["items"])  # без повтора
    assert "п. 5.2.2 и п. 5.2.3" in rasx["detail"] and rasx["status"] == "warn"
    v = inv["verdict"]
    assert v["kind"] == "conflict" and v["confidence"] == "high"
    assert "сверены" in v["why"] and "ГН-7" in v["why"] and v["escalation_id"] == 7


def test_model_flagged_conflict_does_not_claim_verified_quotes():
    out = {"mode": "confident", "answer_kind": "answer", "score": 0.9, "steps": [], "conflict": True,
           "citations": [_cite(1, "p1", "5.2.2")]}
    v = investigation.build(out, [], S)["verdict"]
    assert v["confidence"] == "medium" and "сверены" not in v["why"] and "модель" in v["why"]


def test_not_found_with_escalation_is_gap_and_escalation():
    cands = [_cand("p1", "1.1", 0.01)]
    out = {"mode": "not_found", "score": 0.01, "steps": [], "escalation": {"id": 12}, "citations": [], "variants": []}
    inv = investigation.build(out, cands, S)
    assert inv["evidence"]["items"] == [] and inv["evidence"]["summary"] == "Подходящих пунктов не найдено"
    assert any(i["label"] == "Пробел" for i in inv["check"]["items"])
    v = inv["verdict"]
    assert v["kind"] == "escalation" and v["title"].startswith("Не написано") and "ГН-12" in v["why"]
    assert v["confidence"] == "high"


def test_missing_document_is_named_as_gap():
    out = {"mode": "not_found", "score": 0.2, "steps": [], "missing_docs": ["Регламент контроля поставок"]}
    inv = investigation.build(out, [], S)
    gap = [i for i in inv["check"]["items"] if i["label"] == "Пробел"]
    assert len(gap) == 1 and "Регламент контроля поставок" in gap[0]["detail"]
    assert "Регламент контроля поставок" in inv["verdict"]["why"]


def test_uncertain_variants_low_confidence():
    out = {"mode": "uncertain", "score": 0.3, "steps": [],
           "variants": [{"point_id": "p1", "doc_id": "d1", "clause_no": "2.1", "ref": "Р, п. 2.1", "score": 0.3},
                        {"point_id": "p2", "doc_id": "d1", "clause_no": "2.2", "ref": "Р, п. 2.2", "score": 0.2}]}
    inv = investigation.build(out, [], S)
    assert [i["role"] for i in inv["evidence"]["items"]] == ["variant", "variant"]
    assert inv["verdict"]["kind"] == "variants" and inv["verdict"]["confidence"] == "low"


def test_offtopic_and_refused_have_no_evidence():
    for mode in ("offtopic", "refused"):
        out = {"mode": mode, "steps": [{"step": "guard", "title": "x", "status": "warn", "detail": "Вопрос не о регламентах"}]}
        inv = investigation.build(out, [], S)
        assert inv["evidence"]["summary"] == "Поиск по документам не проводился"
        assert inv["verdict"]["kind"] == mode and inv["verdict"]["score"] is None


def test_ambiguity_lowers_confidence():
    out = {"mode": "confident", "answer_kind": "answer", "score": 0.95, "steps": STEPS_OK,
           "warnings": ["Похожий по смыслу пункт есть и в другом документе: Р2, п. 1"],
           "citations": [_cite(1, "p1", "1.1")]}
    inv = investigation.build(out, [], S)
    assert inv["verdict"]["confidence"] == "medium"
    assert any(i["label"] == "Другая редакция" for i in inv["check"]["items"])


def test_api_returns_investigation_built_from_pipeline(services_factory):
    with TestClient(create_app(services=services_factory())) as c:
        _load(c.app.state.services)
        body = c.post("/api/chat", json={"question": Q_OK}).json()
        inv = body["investigation"]
        assert set(inv) == {"evidence", "check", "verdict"}
        cited = [i for i in inv["evidence"]["items"] if i["role"] == "cited"]
        assert cited and {i["point_id"] for i in cited} == {x["point_id"] for x in body["citations"]}
        assert all(i["link"].startswith("/doc/") for i in inv["evidence"]["items"])
        assert inv["verdict"]["kind"] in ("answer", "quote_only")
        assert c.get("/openapi.json").json()["components"]["schemas"]["InvestigationOut"]
        off = c.post("/api/chat", json={"question": "Где в регламенте про отпуск на Марсе для роботов?"}).json()
        assert off["investigation"]["verdict"]["kind"] in ("not_found", "escalation", "variants")


def test_chatout_accepts_investigation():
    out = {"mode": "confident", "answer_kind": "answer", "score": 0.9, "steps": STEPS_OK,
           "citations": [_cite(1, "p1", "4.2.2")]}
    inv = investigation.build(out, [], S)
    m = ChatOut.model_validate({"answer_id": "a" * 32, "question": "q", "mode": "confident", "text": "t",
                                "score": 0.9, "boxes": [], "route_method": "", "searched_all": False,
                                "ask_feedback": False, "latency_ms": 1, "tokens": {"prompt": 0, "completion": 0},
                                "investigation": inv})
    assert m.investigation.verdict.title == "Ответ найден"


def test_bot_brief_three_lines():
    out = {"mode": "confident", "answer_kind": "answer", "score": 0.89, "steps": STEPS_OK,
           "citations": [_cite(1, "p1", "4.2.2"), _cite(2, "p2", "3.2.1")]}
    inv = investigation.build(out, [_cand("p1", "4.2.2", 0.89), _cand("p2", "3.2.1", 0.7)], S)
    text = investigation_brief(inv)
    lines = text.split("\n")
    assert lines[0] == "Ход работы"
    assert lines[1].startswith("Улики: п. 4.2.2, п. 3.2.1")
    assert lines[2].startswith("Сверка: Всё сходится")
    assert lines[3] == "Вердикт: Ответ найден, уверенность высокая"
    full = format_answer({**out, "text": "Ответ [1].", "investigation": inv})
    assert full.endswith(text)
    assert investigation_brief(None) == ""


def test_bot_offtopic_has_no_brief():
    inv = investigation.build({"mode": "offtopic", "steps": []}, [], S)
    assert format_answer({"mode": "offtopic", "text": "Это не о регламентах.", "investigation": inv}) == \
        "Это не о регламентах."


def test_uncertain_after_model_refusal_does_not_claim_low_score():
    out = {"mode": "uncertain", "score": 0.89, "steps": [], "quote_check": "no_answer",
           "variants": [{"point_id": "p1", "doc_id": "d1", "clause_no": "4.2.2", "ref": "Р, п. 4.2.2"}]}
    v = investigation.build(out, [], S)["verdict"]
    assert "ниже порога" not in v["why"] and "0.89" in v["why"] and "не нашла" in v["why"]
