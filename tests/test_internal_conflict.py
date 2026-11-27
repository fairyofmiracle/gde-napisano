"""Противоречие между пунктами одного документа: принимается только с дословными цитатами из разных пунктов."""
import json

from app.answer import AnswerService, Trace
from app.llm.base import LLMResult
from app.search import Candidate

T1 = "Приоритет не меняется, если не выдержан SLA, нарушения прорабатываются с ответственными."
T2 = "Срок реакции. Если этот срок нарушается, то исполнитель может самостоятельно понизить приоритет."


class _LLM:
    provider = "test"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def chat(self, system, user, json_mode=False):
        return LLMResult(text=json.dumps(self.payload, ensure_ascii=False), prompt_tokens=10, completion_tokens=5,
                         provider="test", model="m")


def _cand(n: str, text: str) -> Candidate:
    return Candidate(f"p{n}", "d1", "Регламент", "dib", n, "", 3, "clause", text)


def _svc(payload: dict) -> AnswerService:
    svc = AnswerService.__new__(AnswerService)
    svc.llm = _LLM(payload)
    return svc


Q = "Меняется ли приоритет заявки, если не выдержан SLA?"


def test_conflict_with_verbatim_quotes_is_accepted():
    ctx = [_cand("5.2.2", T1), _cand("5.2.3", T2)]
    payload = {"conflict": True, "n1": 1, "quote1": T1, "n2": 2,
               "quote2": "то исполнитель может самостоятельно понизить приоритет", "why": "один пункт запрещает смену"}
    out = {"answer_kind": "answer", "citations": [], "text": "x"}
    _svc(payload)._internal_conflict(Q, ctx, out, Trace())
    assert out["conflict"] is True
    assert [c["clause_no"] for c in out["citations"]] == ["5.2.2", "5.2.3"]
    assert "П. 5.2.2" in out["text"] and "П. 5.2.3" in out["text"]
    assert "В одном регламенте есть расхождение" not in out["text"]
    assert "передан" not in out["advice"]
    assert out["advice"] == "Какое правило действует, решает служба качества."


def test_conflict_with_invented_quote_is_rejected():
    ctx = [_cand("5.2.2", T1), _cand("5.2.3", T2)]
    payload = {"conflict": True, "n1": 1, "quote1": T1, "n2": 2, "quote2": "такой фразы в пункте нет совсем"}
    out = {"answer_kind": "answer", "citations": [], "text": "x"}
    _svc(payload)._internal_conflict(Q, ctx, out, Trace())
    assert not out.get("conflict")
    assert out["text"] == "x"


def test_no_conflict_keeps_answer():
    ctx = [_cand("5.2.2", T1), _cand("5.2.3", T2)]
    out = {"answer_kind": "answer", "citations": [], "text": "x"}
    _svc({"conflict": False})._internal_conflict(Q, ctx, out, Trace())
    assert not out.get("conflict")


def test_single_clause_skips_llm():
    out = {"answer_kind": "answer", "citations": [], "text": "x"}
    svc = AnswerService.__new__(AnswerService)
    svc.llm = None  # вызов модели привёл бы к ошибке
    assert svc._internal_conflict(Q, [_cand("5.2.2", T1)], out, Trace()) == (0, 0)


def test_conflict_why_replaces_vs_with_ili():
    ctx = [_cand("5.2.2", T1), _cand("5.2.3", T2)]
    payload = {"conflict": True, "n1": 1, "quote1": T1, "n2": 2,
               "quote2": "то исполнитель может самостоятельно понизить приоритет",
               "why": "приоритет не меняется vs может быть понижен"}
    out = {"answer_kind": "answer", "citations": [], "text": "x"}
    _svc(payload)._internal_conflict(Q, ctx, out, Trace())
    assert out["conflict"] is True
    assert "vs" not in out["text"].lower()
    assert "приоритет не меняется или может быть понижен" in out["text"]
