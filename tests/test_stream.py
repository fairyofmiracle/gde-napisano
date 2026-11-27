"""Ответ по шагам: поток событий, черновик из незаконченного JSON, бот с одной правкой, переформулировка по необходимости."""
from __future__ import annotations

import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from app import progress
from app.llm.base import LLMResult
from app.llm.ollama import OllamaLLM
from app.llm.prompts import REWRITE_SYSTEM
from app.main import create_app
from bot.api import DocagentClient
from bot.service import BotService
from tests.test_agent import Q_OK, RewritingLLM
from tests.test_answers import _load
from tests.test_bot import CONFIDENT, make_settings as bot_settings


def test_draft_extracts_answer_from_unfinished_json():
    ex = progress.AnswerDraft.extract
    assert ex('{"answerable": true, "ans') is None
    assert ex('{"answerable": true, "answer": "Отпуск 28 дн') == "Отпуск 28 дн"
    assert ex('{"answer": "Сказано: \\"да\\" [1]\\nи всё", "used": [1]}') == 'Сказано: "да" [1]\nи всё'
    assert ex('{"answer": "обрыв на слэше \\') == "обрыв на слэше "
    assert ex('{"answer": "\\u0414\\u0430"}') == "Да"


def test_draft_emits_throttled_and_flushes_last_text():
    got = []
    t = [0.0]
    d = progress.AnswerDraft(interval=1.0, clock=lambda: t[0])
    with progress.listening(got.append):
        d.feed('{"answer": "Раз')
        d.feed(" два")
        t[0] = 2.0
        d.feed(" три")
        d.feed('", "used": [1]}')
        d.flush()
    texts = [e["text"] for e in got if e["type"] == "draft"]
    assert texts[0] == "Раз" and texts[-1] == "Раз два три"
    assert len(texts) <= 3


def test_events_are_silent_without_listener():
    progress.emit("status", text="никому")  # не падает и ничего не делает
    assert not progress.active()


def _events(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_stream_endpoint_sends_evidence_before_final(services_factory):
    with TestClient(create_app(services=services_factory())) as c:
        _load(c.app.state.services)
        r = c.post("/api/chat/stream", json={"question": Q_OK})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        assert r.headers.get("x-accel-buffering") == "no"
        ev = _events(r)
        kinds = [e["type"] for e in ev]
        assert kinds[0] == "status" and kinds[-1] == "final"
        assert "evidence" in kinds and kinds.index("evidence") < kinds.index("final")
        evidence = next(e for e in ev if e["type"] == "evidence")
        assert evidence["items"] and evidence["items"][0]["ref"] and evidence["items"][0]["quote"]
        assert any(e["type"] == "status" and e["text"] == "Пишу ответ" for e in ev)
        final = ev[-1]["answer"]
        plain = c.post("/api/chat", json={"question": Q_OK}).json()
        assert final["mode"] == plain["mode"] == "confident"
        assert [x["clause_no"] for x in final["citations"]] == [x["clause_no"] for x in plain["citations"]]
        assert "_candidates" not in final


def test_stream_endpoint_respects_rate_limit(services_factory):
    with TestClient(create_app(services=services_factory(rate_limit_per_minute=1))) as c:
        _load(c.app.state.services)
        assert c.post("/api/chat/stream", json={"question": Q_OK}).status_code == 200
        r = c.post("/api/chat/stream", json={"question": Q_OK})
        assert r.status_code == 429 and r.headers["retry-after"] == "60"


def test_llm_rewrite_skipped_when_nothing_close_was_found(services_factory):
    llm = RewritingLLM("Продолжительность ежегодного оплачиваемого отпуска календарных дней")
    svc = services_factory(llm=llm, agent_rewrite_llm_min_score=0.99)
    _load(svc)
    ans = svc.answers.ask("сколько стоит парковка у офиса для гостей компании в выходные дни")
    assert not any(s == REWRITE_SYSTEM for s, _ in llm.calls)
    rw = [s for s in ans["steps"] if s["step"] == "rewrite"]
    assert not rw or "Модель" not in rw[0]["detail"]


def test_pronoun_inside_question_does_not_force_llm_rewrite(services_factory):
    llm = RewritingLLM("что угодно")
    svc = services_factory(llm=llm, agent_rewrite_llm_min_score=0.99)
    assert not svc.answers._needs_llm_rewrite("Сколько суточных в командировке и как они считаются в выходной?", 0.1)
    assert svc.answers._needs_llm_rewrite("А он распространяется на субподрядчиков тоже?", 0.1)
    assert svc.answers._needs_llm_rewrite("Сколько суточных в командировке и как они считаются в выходной?", 0.995)


def test_llm_rewrite_kept_for_short_questions(services_factory):
    llm = RewritingLLM("Продолжительность ежегодного оплачиваемого отпуска календарных дней")
    svc = services_factory(llm=llm, agent_rewrite_llm_min_score=0.99)
    _load(svc)
    ans = svc.answers.ask("как долго отдыхать летом")
    assert any(s == REWRITE_SYSTEM for s, _ in llm.calls)
    assert ans["iterations"] == 1


def test_ollama_stream_collects_text_and_tokens(monkeypatch):
    lines = [{"message": {"content": '{"answer": "Да'}}, {"message": {"content": ' [1]"}'}},
             {"done": True, "prompt_eval_count": 120, "eval_count": 7}]

    class Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            return (json.dumps(x) for x in lines)

    monkeypatch.setattr("app.llm.ollama.httpx.stream", lambda *a, **kw: Resp())

    class S:
        ollama_base_url, ollama_model, ollama_num_ctx = "http://x", "m", 2048
        llm_temperature, llm_max_tokens, ollama_timeout = 0.1, 100, 5

    pieces = []
    res = OllamaLLM(S()).chat_stream("sys", "user", json_mode=True, on_delta=pieces.append)
    assert isinstance(res, LLMResult)
    assert res.text == '{"answer": "Да [1]"}' and res.prompt_tokens == 120 and res.completion_tokens == 7
    assert "".join(pieces) == res.text


def test_bot_sends_found_clauses_then_edits_with_answer():
    body = "\n".join([json.dumps({"type": "status", "text": "Ищу в документах"}),
                      json.dumps({"type": "evidence", "items": [{"ref": "x", "clause_no": "3.1",
                                                                  "doc_title": "Положение об отпусках"}]}),
                      json.dumps({"type": "draft", "text": "Черновик"}),
                      json.dumps({"type": "final", "answer": CONFIDENT})]) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat/stream"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "application/x-ndjson"})

    api = DocagentClient("http://app:8000", transport=httpx.MockTransport(handler))
    sent, edits = [], []

    async def reply(text, buttons=None):
        sent.append(text)

    async def send(text):
        sent.append(text)
        return "handle"

    async def edit(handle, text, buttons=None):
        edits.append((handle, text, buttons))
        return True

    reply.send, reply.edit = send, edit
    svc = BotService(bot_settings(), api, None)
    asyncio.run(svc.handle_text(1, "Сколько дней отпуска?", reply))
    assert sent and sent[0].startswith("Нашла пункты: п. 3.1 (Положение об отпусках). Пишу ответ")
    assert len(edits) == 1 and edits[0][0] == "handle" and "Источники:" in edits[0][1]
    assert len(sent) == 1  # без лишних сообщений


def test_bot_without_edit_support_answers_as_before():
    api = DocagentClient("http://app:8000", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=CONFIDENT)))
    out = []

    async def reply(text, buttons=None):
        out.append(text)

    asyncio.run(BotService(bot_settings(), api, None).handle_text(1, "Сколько дней отпуска?", reply))
    assert len(out) == 1 and "Источники:" in out[0]
