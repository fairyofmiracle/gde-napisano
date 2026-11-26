from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.answer import verify_quote
from app.index.models import FakeReranker, sigmoid
from app.llm import LLMConfigError, make_llm
from app.llm.fake import FakeLLM
from app.main import create_app

DOCS = {
    "hr/Положение_об_отпусках.txt": """Положение об отпусках
3. Ежегодный отпуск
3.1. Продолжительность ежегодного оплачиваемого отпуска составляет 28 календарных дней.
3.3. Заявление на ежегодный отпуск подаётся руководителю за 14 календарных дней до начала отпуска.
""",
    "it/Инструкция_ИТ.txt": """Инструкция ИТ
2. Пароли
2.1. Если вы забыли пароль от корпоративной почты, позвоните на линию поддержки 1234.
2.2. Пароль меняется каждые 90 дней.
""",
}


def _load(svc):
    for rel, text in DOCS.items():
        p = svc.indexer.docs_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    svc.indexer.sync()


def test_sigmoid_and_fake_reranker_in_unit_interval():
    for x in (-1e6, -50, -1, 0, 1, 50, 1e6):
        assert 0.0 <= sigmoid(x) <= 1.0
    scores = FakeReranker().score("отпуск 28 дней", ["отпуск 28 дней", "", "совсем другое", "отпуск " * 100])
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_search_scores_in_unit_interval_even_with_memory(services_factory):
    svc = services_factory(memory_boost=1.0, memory_penalty=1.0)
    _load(svc)
    ans = svc.answers.ask("Сколько календарных дней ежегодного отпуска?")
    for _ in range(5):
        svc.memory.add_feedback(ans["answer_id"], True, ans["citations"][0]["doc_id"], "x",
                                ans["citations"][0]["clause_no"])
    res = svc.searcher.search("Сколько календарных дней ежегодного отпуска?")
    assert res.candidates
    assert all(0.0 <= c.score <= 1.0 and 0.0 <= c.rerank_score <= 1.0 for c in res.candidates)


def test_confident_answer_has_verified_quote(services_factory):
    svc = services_factory()
    _load(svc)
    ans = svc.answers.ask("Сколько календарных дней ежегодного оплачиваемого отпуска?")
    assert ans["mode"] == "confident"
    c = ans["citations"][0]
    assert c["verified"] and c["clause_no"] == "3.1"
    assert c["ref"].startswith("Положение об отпусках, п. 3.1")
    assert "28 календарных дней" in c["quote"]
    assert ans["owner"]["box"] == "hr"
    assert ans["tokens"]["prompt"] > 0


def test_unverified_quote_downgrades_to_variants(services_factory):
    svc = services_factory(llm=FakeLLM(mode="wrong_quote"))
    _load(svc)
    ans = svc.answers.ask("Сколько календарных дней ежегодного оплачиваемого отпуска?")
    assert ans["mode"] == "uncertain"
    assert ans["quote_check"] == "failed"
    assert 2 <= len(ans["variants"]) <= 3
    assert ans["ask_feedback"] is True


def test_uncertain_mode_offers_variants(services_factory):
    svc = services_factory()
    _load(svc)
    ans = svc.answers.ask("пароль и отпуск")  # половина слов совпадает с разными пунктами
    assert ans["mode"] == "uncertain", ans["score"]
    assert 2 <= len(ans["variants"]) <= 3
    assert "это то, что вы искали" in ans["text"].lower()
    assert ans["ask_feedback"] is True


def test_not_found_is_honest_and_shows_owner(services_factory):
    svc = services_factory()
    _load(svc)
    ans = svc.answers.ask("Можно ли приводить собаку в офис?")
    assert ans["mode"] == "not_found"
    assert "нет" in ans["text"]
    assert ans["citations"] == [] and ans["variants"] == []
    assert ans["ask_feedback"] is True


def test_feedback_sampling_about_one_in_fifty(services_factory):
    svc = services_factory(rng=random.Random(42))
    _load(svc)
    asked = sum(svc.answers.ask("Сколько календарных дней ежегодного оплачиваемого отпуска?")["ask_feedback"]
                for _ in range(200))
    assert 0 < asked < 15


def test_memory_promotes_confirmed_clause(services_factory):
    svc = services_factory(confident_threshold=0.95)
    _load(svc)
    q = "пароль от почты отпуск"
    first = svc.answers.ask(q)
    target = next(v for v in first["variants"] if v["clause_no"] == "2.1")
    before = target["score"]
    with TestClient(create_app(services=svc)) as c:
        r = c.post("/api/feedback", json={"answer_id": first["answer_id"], "helpful": True,
                                          "point_id": target["point_id"]})
        assert r.status_code == 200
        second = svc.answers.ask(q)
        top = second["variants"][0]
        assert top["clause_no"] == "2.1" and top["score"] > before
        # «нет» по другому пункту опускает его
        other = next(v for v in second["variants"] if v["clause_no"] != "2.1")
        c.post("/api/feedback", json={"answer_id": second["answer_id"], "helpful": False,
                                      "point_id": other["point_id"]})
        third = svc.answers.ask(q)
        lowered = next((v for v in third["variants"] if v["point_id"] == other["point_id"]), None)
        assert lowered is None or lowered["score"] < other["score"]


def test_practice_is_saved_and_shown(services_factory):
    svc = services_factory()
    _load(svc)
    with TestClient(create_app(services=svc)) as c:
        ans = c.post("/api/chat", json={"question": "Сколько календарных дней ежегодного оплачиваемого отпуска?"}).json()
        assert ans["mode"] == "confident"
        r = c.post("/api/feedback", json={"answer_id": ans["answer_id"], "helpful": False})
        assert r.json()["ask_practice"] is True
        r = c.post("/api/practice", json={"answer_id": ans["answer_id"],
                                          "text": "Делили отпуск на две части по 14 дней через кадры"})
        assert r.status_code == 200
        cited = ans["citations"][0]
        assert cited["clause_no"] == "3.1"
        # «нет» опустил пункт 3.1 для такого же вопроса — память работает
        again = c.post("/api/chat", json={"question": "Сколько календарных дней ежегодного оплачиваемого отпуска?"}).json()
        assert not again["citations"] or again["citations"][0]["clause_no"] != "3.1" or again["score"] < ans["score"]
        # кейс практики привязан к пункту 3.1 и показывается вместе с ним
        clause = c.get(f"/api/clauses/{cited['point_id']}").json()
        assert clause["clause_no"] == "3.1" and clause["context"]
        # по умолчанию практика видна только после одобрения оператором
        assert not any("14 дней" in p["text"] for p in clause["practice"])
        svc.db.execute("UPDATE practice SET status='approved' WHERE id=?", (r.json()["id"],))
        clause = c.get(f"/api/clauses/{cited['point_id']}").json()
        assert any("14 дней" in p["text"] for p in clause["practice"])
        assert svc.memory.practice_for(cited["doc_id"], "3.1")
        # нельзя оставить отзыв на пункт, которого не было в ответе
        bad = c.post("/api/feedback", json={"answer_id": ans["answer_id"], "helpful": True, "point_id": "nope"})
        assert bad.status_code == 400


def test_llm_switch_and_explicit_gigachat_error(tmp_path):
    from tests.conftest import make_settings

    assert make_llm(make_settings(tmp_path, llm_provider="ollama")).provider == "ollama"
    assert make_llm(make_settings(tmp_path, llm_provider="fake")).provider == "fake"
    with pytest.raises(LLMConfigError, match="GIGACHAT_CREDENTIALS"):
        make_llm(make_settings(tmp_path, llm_provider="gigachat", gigachat_credentials=""))
    llm = make_llm(make_settings(tmp_path, llm_provider="gigachat", gigachat_credentials="abc"))
    assert llm.provider == "gigachat"


def test_ollama_unavailable_falls_back_to_quote_only(services_factory):
    from app.llm.ollama import OllamaLLM
    from tests.conftest import make_settings

    class Dead(OllamaLLM):
        pass

    svc = services_factory()
    _load(svc)
    s = make_settings(svc.settings.data_dir.parent, ollama_base_url="http://127.0.0.1:9", ollama_timeout=1)
    svc.answers.llm = Dead(s)
    ans = svc.answers.ask("Сколько календарных дней ежегодного оплачиваемого отпуска?")
    # модель недоступна: показываем дословно лучший пункт, а не текст внутренней ошибки
    assert ans["mode"] == "confident" and ans["answer_kind"] == "quote_only"
    assert ans["llm_error"] == "llm_unavailable"
    assert "127.0.0.1" not in str(ans) and "Connection" not in str(ans)
    assert ans["citations"][0]["clause_no"] == "3.1" and "28 календарных дней" in ans["citations"][0]["quote"]
    assert any(st["step"] == "generate" and st["status"] == "fail" for st in ans["steps"])


@pytest.mark.parametrize("quote,ok", [
    ("составляет 28 календарных дней", True),
    ("Продолжительность ежегодного оплачиваемого отпуска составляет 28 календарных дней.", True),
    ("«составляет 28 КАЛЕНДАРНЫХ дней»", True),
    ("составляет 30 календарных дней", False),
    ("отпуск можно перенести на следующий год", False),
])
def test_verify_quote(quote, ok):
    src = "Продолжительность ежегодного оплачиваемого отпуска составляет 28 календарных дней."
    assert verify_quote(quote, src) is ok


def test_verbatim_part_trims_paraphrase_to_source_text():
    from app.answer import verbatim_part

    src = "К авансовому отчёту прилагаются билеты, посадочные талоны, счета за проживание и кассовые чеки."
    part = verbatim_part("Документы для отчёта: билеты, посадочные талоны, счета за проживание и кассовые чеки.", src)
    assert part and part in src and part.startswith("билеты")
    assert verbatim_part("Совсем другой текст про отпуск и пропуска", src) is None
