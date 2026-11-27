"""Пункты, на которые ссылается ответ из нескольких частей, и сокращение пункта для запроса к модели."""
from app.answer import AnswerService, clip_text
from app.search import Candidate


def _cand(n: str, text: str) -> Candidate:
    return Candidate(f"p{n}", "d1", "Регламент", "dib", n, "", 3, "clause", text)


def test_complete_citations_adds_referenced_clause():
    svc = AnswerService.__new__(AnswerService)
    ctx = [_cand("8.3", "Срок 6 месяцев. Далее текст."), _cand("5.2.3", "Максимум 10 заявок.")]
    have = [{"n": 2, "quote": "Максимум 10 заявок.", "ref": "r2"}]
    out = svc._complete_citations({"used": [1, 2]}, "10 [2], 6 месяцев [1]", ctx, have)
    assert [c["n"] for c in out] == [2, 1]
    assert out[1]["quote"] == "Срок 6 месяцев."
    assert out[1]["auto_quote"] is True


def test_complete_citations_ignores_unknown_refs():
    svc = AnswerService.__new__(AnswerService)
    out = svc._complete_citations({"used": ["x", 7]}, "ответ [9]", [_cand("1", "Текст.")], [])
    assert out == []


def test_clip_text_cuts_on_sentence_and_keeps_short_text():
    assert clip_text("Коротко.", 900) == "Коротко."
    text = "Первое предложение довольно длинное. " + "Второе " * 50
    assert clip_text(text, 60) == "Первое предложение довольно длинное."
    assert clip_text(text, 0) == text
