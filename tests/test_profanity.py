from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.fake import FakeLLM
from app.main import create_app
from app.profanity import REFUSAL_TEXT, ProfanityFilter, contains_profanity

OBSCENE = [
    # русский, прямые формы
    "хуй", "Ты хуйню несёшь", "ПИЗДЕЦ", "пизда", "сука", "блять", "блядь", "бля", "ёбаный в рот",
    "ебать", "заебись", "нахуй", "нахер", "похер", "охуенно", "какого хуя", "мудак", "пидор",
    "залупа", "жопа", "долбоёб", "уебан", "съебал", "проебал", "ебучий", "говно", "гавно", "срать",
    "шлюха", "дрочить", "еб твою мать", "хуета", "херня", "ублюдок", "гандон", "нах",
    # ё/е, регистр, растянутые буквы
    "СУУУКА", "хуууууй", "пиздееец", "бляяяять", "жооопа",
    # разделители между буквами
    "х.у.й", "х у й", "х-у-й", "х_у_й", "п и з д а", "пиз-да", "бл.я.ть",
    # маскировка символами
    "п*зда", "бл@дь", "бл*ть", "х*й", "ж*па", "**хуй**", "еб@ть",
    # латиница, похожая на кириллицу, и цифры
    "xуй", "хyй", "пиздa", "сyка", "пiзда", "п1зда",
    # невидимые символы и ударения
    "ху\u200bй", "ху\u0301й", "пи\u00adзда",
    # английский
    "fuck", "FUCK you", "f*ck", "f@ck", "sh1t", "motherfucker", "bullshit", "asshole", "bitch", "cunt",
    "what the fucking hell", "wtf",
    # русский мат латиницей
    "blyat", "cyka blyat", "pizdec", "suka", "nahuy", "pohui", "govno", "ebat",
]

CLEAN = [
    # слова из ТЗ
    "страхование", "оскорбление", "истреблять", "рубль", "хлеб", "суббота", "бляха", "бляха-муха",
    # похожие корни и фрагменты
    "страхует", "застрахуй", "Олеговна", "саговник", "агавный", "всеблагой", "Херсон", "херувим", "хрен",
    "учеба", "себя", "себестоимость", "Себастьян", "небо", "ребенок", "проблема", "употребление",
    "сучок", "сукно", "мандарин", "мандат", "шлюз", "мудрость", "мудрый", "страна", "Сретение",
    "хула", "хуже", "худой", "хутор", "ссора", "ассистент", "аппарат", "массаж", "договор", "заговор",
    "поговорка", "подъезд", "гребень", "заём", "блея", "бляшка", "дрожь", "дробь", "Лондон", "пиджак",
    "scunthorpe", "assassin", "class", "shiitake", "ebay", "Eban", "Huy", "Huynh", "bleat", "Sebastian",
    "muddily", "pizza", "Hello world", "grass",
    # обычные вопросы и служебные символы
    "Сколько календарных дней ежегодного оплачиваемого отпуска?",
    "Как оформить страхование командировки и подать заявление на отпуск",
    "Что делать, если забыл пароль от почты? Позвоните на 1234.",
    "Цена 50$ и скидка 100% по договору", "Пишите на ivan.petrov@mail.ru",
    "Положение об оскорблении чести и достоинства", "ИП Иванов, ул. Ленина, д. 5, оф. 7",
    "п. 3.1 и п. 3.3 Положения",
]


@pytest.mark.parametrize("text", OBSCENE)
def test_obscene_detected(text):
    assert contains_profanity(text), text


@pytest.mark.parametrize("text", CLEAN)
def test_clean_not_flagged(text):
    assert not contains_profanity(text), text


def test_empty_and_whitespace():
    assert not contains_profanity("")
    assert not contains_profanity("   \n\t ")


def test_allowlist_exempts_word():
    assert contains_profanity("сучка")
    assert not contains_profanity("сучка", allow=frozenset({"сучка"}))
    assert not contains_profanity("СУЧКА", allow=frozenset({"сучка"}))


def test_filter_from_settings(tmp_path):
    s = Settings(_env_file=None, profanity_filter=True, profanity_allowlist=" Сучка , херр ")
    f = ProfanityFilter.from_settings(s)
    assert f.enabled and f.allow == {"сучка", "херр"}
    assert not f.check("сучка")
    assert f.check("хуй")
    off = ProfanityFilter.from_settings(Settings(_env_file=None, profanity_filter=False))
    assert not off.check("хуй")


@pytest.mark.parametrize("raw,expected", [("on", True), ("off", False), ("true", True), ("false", False)])
def test_env_switch(monkeypatch, raw, expected):
    monkeypatch.setenv("PROFANITY_FILTER", raw)
    assert Settings(_env_file=None).profanity_filter is expected


def test_env_default_is_on(monkeypatch):
    monkeypatch.delenv("PROFANITY_FILTER", raising=False)
    assert Settings(_env_file=None).profanity_filter is True


class _NoCallLLM(FakeLLM):
    def chat(self, system, user, json_mode=False):
        raise AssertionError("языковая модель не должна вызываться")


def _client(services_factory, **kw):
    svc = services_factory(llm=_NoCallLLM(), **kw)
    return svc, TestClient(create_app(services=svc))


def test_answer_service_refuses_without_search_and_model(services_factory, caplog):
    svc = services_factory(llm=_NoCallLLM())
    svc.searcher.search = lambda *a, **k: (_ for _ in ()).throw(AssertionError("поиск не должен выполняться"))
    with caplog.at_level(logging.DEBUG):
        out = svc.answers.ask("Где мой отпуск, сука?")
    assert out["mode"] == "refused"
    assert out["text"] == REFUSAL_TEXT
    assert out["question"] == ""
    assert out["citations"] == [] and out["variants"] == [] and out["ask_feedback"] is False
    assert out["tokens"] == {"prompt": 0, "completion": 0}
    # в базу ничего не записано, в лог не попал текст вопроса
    assert svc.db.query("SELECT COUNT(*) n FROM answers")[0]["n"] == 0
    assert "сука" not in caplog.text.lower() and "отпуск" not in caplog.text.lower()
    assert "отклонён" in caplog.text


def test_chat_api_unified_refusal(services_factory):
    svc, c = _client(services_factory)
    with c:
        r = c.post("/api/chat", json={"question": "это пиздец какой-то, где регламент"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "refused"
        assert body["text"] == REFUSAL_TEXT
        assert body["route_method"] == "profanity_filter"
        assert set(body) >= {"answer_id", "question", "mode", "text", "score", "boxes", "citations",
                             "variants", "ask_feedback", "latency_ms", "tokens"}
        assert "😀" not in body["text"] and body["text"].isprintable()
        # отзыв на отказ не принимается: ответ не сохранялся
        fb = c.post("/api/feedback", json={"answer_id": body["answer_id"], "helpful": False})
        assert fb.status_code in (400, 404)


def test_chat_api_normal_question_still_goes_to_model(services_factory):
    svc = services_factory()
    with TestClient(create_app(services=svc)) as c:
        r = c.post("/api/chat", json={"question": "Сколько дней отпуска положено по договору страхования?"})
        assert r.status_code == 200
        assert r.json()["mode"] != "refused"


def test_filter_off_passes_question_on(services_factory):
    svc = services_factory(profanity_filter=False)
    with TestClient(create_app(services=svc)) as c:
        r = c.post("/api/chat", json={"question": "это пиздец какой-то, где регламент"})
        assert r.status_code == 200
        assert r.json()["mode"] in ("confident", "uncertain", "not_found")


def test_openapi_lists_refused_mode(services_factory):
    svc = services_factory()
    with TestClient(create_app(services=svc)) as c:
        schema = c.get("/openapi.json").json()
        assert "refused" in str(schema["components"]["schemas"]["ChatOut"]["properties"]["mode"])


def test_practice_text_with_profanity_rejected(services_factory):
    svc = services_factory()
    with TestClient(create_app(services=svc)) as c:
        r = c.post("/api/practice", json={"answer_id": "c0ffee00c0ffee00", "text": "решили через жопу"})
        assert r.status_code == 400
        assert "нецензурную" in r.json()["error"]["message"]
