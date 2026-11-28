"""Агент-проверяющий: принять ответ, один повторный поиск, передача в службу качества, сбой без вреда, выключен."""
from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from app.llm import LLMError
from app.llm.base import LLMResult
from app.llm.fake import FakeLLM
from app.llm.prompts import VERIFIER_SYSTEM
from app.main import create_app
from tests.test_agent import Q_OK
from tests.test_answers import _load


class VerifyingLLM(FakeLLM):
    """FakeLLM отвечает как раньше, а на запрос проверяющего — заданным вердиктом (по очереди)."""

    def __init__(self, *verdicts, delay: float = 0.0) -> None:
        super().__init__()
        self.verdicts = list(verdicts)
        self.delay = delay
        self.verifier_calls = 0
        self.queries: list[str] = []

    def chat(self, system, user, json_mode=False):
        if system != VERIFIER_SYSTEM:
            return super().chat(system, user, json_mode)
        self.verifier_calls += 1
        if self.delay:
            time.sleep(self.delay)
        v = self.verdicts.pop(0) if self.verdicts else {"verdict": "ok"}
        if isinstance(v, Exception):
            raise v
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        return LLMResult(text, 120, 15, "fake", "fake")


def _svc(services_factory, llm, **kw):
    svc = services_factory(llm=llm, verifier_enabled=True, **kw)
    _load(svc)
    return svc


def _step(ans, name="verifier"):
    return [st for st in ans["steps"] if st["step"] == name]


def test_disabled_by_default_makes_no_extra_call(services_factory):
    llm = VerifyingLLM({"verdict": "escalate", "reason": "x"})
    svc = services_factory(llm=llm)
    _load(svc)
    ans = svc.assistant.ask(Q_OK)
    assert ans["mode"] == "confident"
    assert llm.verifier_calls == 0
    assert not _step(ans) and not ans.get("escalation")


def test_ok_keeps_answer_and_adds_tokens(services_factory):
    base = services_factory(llm=FakeLLM())
    _load(base)
    plain = base.answers.ask(Q_OK)
    llm = VerifyingLLM({"verdict": "ok", "reason": "срок совпадает"})
    svc = _svc(services_factory, llm)
    ans = svc.answers.ask(Q_OK)
    assert llm.verifier_calls == 1
    assert ans["mode"] == "confident" and ans["text"] == plain["text"]
    assert ans["verifier"]["verdict"] == "ok"
    assert ans["tokens"]["prompt"] == plain["tokens"]["prompt"] + 120
    st = _step(ans)
    assert st and st[0]["status"] == "ok"
    assert ans["steps"][-1]["step"] == "decide"
    assert "_rec" not in ans


def test_retry_runs_one_more_search(services_factory):
    llm = VerifyingLLM({"verdict": "retry", "query": "продолжительность ежегодного отпуска", "reason": "не о том"},
                       {"verdict": "escalate", "reason": "второй вызов не должен случиться"})
    svc = _svc(services_factory, llm)
    ans = svc.answers.ask(Q_OK)
    assert llm.verifier_calls == 1  # не больше одной итерации
    assert ans["verifier"]["verdict"] == "retry"
    assert ans["mode"] == "confident" and ans["citations"]
    titles = [st["title"] for st in ans["steps"]]
    assert any(t.startswith("Повтор: ") for t in titles) or any("оставлен исходный" in st["detail"]
                                                                for st in _step(ans))
    assert ans["steps"][-1]["step"] == "decide"


def test_escalate_goes_to_quality_service(services_factory):
    llm = VerifyingLLM({"verdict": "escalate", "reason": "срок в ответе не совпадает с пунктом"})
    svc = _svc(services_factory, llm)
    ans = svc.assistant.ask(Q_OK)
    assert ans["mode"] == "confident" and ans["citations"]  # ответ и цитаты остаются видны
    assert ans["escalation"] and ans["escalation"]["id"]
    assert "Проверяющий агент" in ans["notice"] and "ГН-" in ans["notice"]
    assert "verifier_doubt" not in ans
    st = _step(ans)
    assert st and st[0]["status"] == "warn"
    assert any(i["label"] == "Проверяющий агент" for i in ans["investigation"]["check"]["items"])


def test_fail_open_on_error_bad_json_and_timeout(services_factory):
    for verdict, kw in ((LLMError("down"), {}), ("не json", {}), ({"verdict": "может быть"}, {}),
                        ({"verdict": "retry", "query": ""}, {}), ({"verdict": "escalate"}, {"delay": 1.5})):
        llm = VerifyingLLM(verdict, **kw)
        svc = _svc(services_factory, llm, verifier_timeout=0.5)
        t = time.perf_counter()
        ans = svc.assistant.ask(Q_OK)
        assert time.perf_counter() - t < 1.4
        assert ans["mode"] == "confident" and ans["citations"]
        assert not ans.get("escalation")
        assert ans["verifier"]["verdict"] == "skipped"
        assert _step(ans)[0]["status"] == "skip"


def test_stream_ends_with_final_and_verifier_status(services_factory):
    llm = VerifyingLLM({"verdict": "ok"})
    with TestClient(create_app(services=services_factory(llm=llm, verifier_enabled=True))) as c:
        _load(c.app.state.services)
        r = c.post("/api/chat/stream", json={"question": Q_OK})
        ev = [json.loads(x) for x in r.text.splitlines() if x.strip()]
        assert ev[-1]["type"] == "final"
        assert any(e["type"] == "status" and e.get("step") == "verifier" for e in ev)
        assert ev[-1]["answer"]["verifier"]["verdict"] == "ok"
