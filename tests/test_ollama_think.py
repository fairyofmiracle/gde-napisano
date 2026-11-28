"""Отключение размышлений у локальных моделей (Qwen3) в клиенте Ollama."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.llm.ollama import OllamaLLM, ThinkFilter, strip_think


def _settings(think=None):
    class S:
        ollama_base_url, ollama_model, ollama_num_ctx = "http://x", "qwen3:1.7b", 2048
        llm_temperature, llm_max_tokens, ollama_timeout = 0.1, 100, 5
        ollama_think = think
    return S()


class _StreamResp:
    def __init__(self, status: int, lines=(), body: str = ""):
        self.status_code, self._lines, self._body = status, list(lines), body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body.encode()

    def iter_lines(self):
        return (json.dumps(x) for x in self._lines)


def test_setting_parsed_from_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_THINK", "false")
    assert Settings(_env_file=None).ollama_think is False
    monkeypatch.delenv("OLLAMA_THINK")
    assert Settings(_env_file=None).ollama_think is None


def test_payload_has_think_only_when_set():
    assert "think" not in OllamaLLM(_settings())._payload("s", "u", False, False)
    p = OllamaLLM(_settings(False))._payload("s", "u", True, True)
    assert p["think"] is False and p["format"] == "json" and p["messages"][0]["content"] == "s"


@pytest.mark.parametrize("raw,clean", [
    ("<think>\nдумаю\n</think>\n\n{\"answer\": \"Да\"}", "{\"answer\": \"Да\"}"),
    ("<think></think>Ответ", "Ответ"),
    ("Ответ без блока", "Ответ без блока"),
    ("<think>оборвалось на полуслове", ""),
])
def test_strip_think(raw, clean):
    assert strip_think(raw) == clean


def test_filter_handles_tags_split_between_chunks():
    f = ThinkFilter()
    chunks = ["<th", "ink>раз", "мышление</th", "ink>\n", '{"answer": ', '"5 дней <', 'b>"}']
    out = "".join(f.feed(c) for c in chunks) + f.flush()
    assert out == '{"answer": "5 дней <b>"}'


def test_chat_sends_think_false_and_strips_block(monkeypatch):
    seen = {}

    def post(url, json=None, timeout=None):
        seen.update(json)
        return httpx.Response(200, json={"message": {"content": "<think>\n\n</think>\n\nДа"},
                                         "prompt_eval_count": 10, "eval_count": 3},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr("app.llm.ollama.httpx.post", post)
    res = OllamaLLM(_settings(False)).chat("s", "u")
    assert seen["think"] is False and res.text == "Да" and res.completion_tokens == 3


def test_chat_falls_back_to_prompt_hint_when_think_rejected(monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(json)
        req = httpx.Request("POST", url)
        if "think" in json:
            return httpx.Response(400, json={"error": "invalid think value"}, request=req)
        return httpx.Response(200, json={"message": {"content": "Да"}}, request=req)

    monkeypatch.setattr("app.llm.ollama.httpx.post", post)
    llm = OllamaLLM(_settings(False))
    assert llm.chat("s", "u").text == "Да"
    assert len(calls) == 2 and calls[1]["messages"][0]["content"].endswith("/no_think")
    llm.chat("s", "u")
    assert len(calls) == 3 and "think" not in calls[2]


def test_stream_hides_thinking_and_falls_back(monkeypatch):
    responses = iter([
        _StreamResp(400, body='{"error":"think not supported"}'),
        _StreamResp(200, [{"message": {"content": "<think>"}}, {"message": {"thinking": "x", "content": "долго"}},
                          {"message": {"content": "</think>"}}, {"message": {"content": '{"answer": "Да"}'}},
                          {"done": True, "prompt_eval_count": 50, "eval_count": 9}]),
    ])
    payloads = []

    def stream(method, url, json=None, timeout=None):
        payloads.append(json)
        return next(responses)

    monkeypatch.setattr("app.llm.ollama.httpx.stream", stream)
    pieces = []
    res = OllamaLLM(_settings(False)).chat_stream("s", "u", json_mode=True, on_delta=pieces.append)
    assert res.text == '{"answer": "Да"}' and "".join(pieces) == res.text
    assert res.prompt_tokens == 50 and "think" not in payloads[1]


def test_other_errors_not_retried(monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(1)
        return httpx.Response(404, text="model not found", request=httpx.Request("POST", url))

    monkeypatch.setattr("app.llm.ollama.httpx.post", post)
    from app.llm.base import LLMError
    with pytest.raises(LLMError):
        OllamaLLM(_settings(False)).chat("s", "u")
    assert len(calls) == 1
