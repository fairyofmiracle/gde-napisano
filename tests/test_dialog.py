"""Сценарии заказчика: эскалация письмом, дообучение через ответ эксперта, уточнение подразделения,
стиль ответа со ссылками, отказ вне базы, глоссарий, аналитика. Только заглушки моделей и outbox:
никаких реальных писем на внешние адреса."""
from __future__ import annotations

import email
import email.policy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.escalation import extract_reply, is_final_answer
from app.glossary import Glossary
from app.guard import missing_documents, off_topic
from app.main import create_app
from tests.conftest import ROOT

DOCS = {
    "Проектная деятельность ДБРЦТ.txt": """# 1. Инициация заявки
1.5 Заявка на расчёт должна быть инициирована не менее чем за 3 рабочих дня до подачи предложения заказчику.
# 4. Субподрядчики
4.1 Архитектор направляет запрос на поиск подрядчика, поиск идёт в соответствии с регламентом подбора субподрядчика.
# 10. Смета
10.2 Руководитель проекта создаёт смету проекта в течение 3 рабочих дней после подписания договора.
""",
    "Организация проектной деятельности Тех дирекции.txt": """# 1. Инициация расчёта
1.5 Расчёт заявки должен быть инициирован за 2 недели до подачи конкурса, иначе заявка отклоняется.
# 16. Субподрядчики
16.1 За подготовку и подписание договора с субподрядчиком отвечает Логист.
""",
    "Проектная деятельность Дирекции ИБ.txt": """# 14. Субподрядчики
14.1 За заключение договора с субподрядчиком отвечает РП, он привлекает менеджера по договорам.
# 24. Гарантия
24.7 Гарантийный срок исполнителя составляет 2 года с момента подписания акта выполненных работ.
""",
}
QUALITY = "quality-test@example.local"
USER = "employee@example.local"


@pytest.fixture
def svc(services_factory, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    for name, text in DOCS.items():
        (docs / name).write_text(text, encoding="utf-8")
    s = services_factory(boxes_csv=ROOT / "examples" / "boxes_softline.csv",
                         glossary_tsv=ROOT / "examples" / "glossary.tsv", quality_email=QUALITY,
                         demo_user_email=USER, smtp_host="", imap_host="", confident_threshold=0.6,
                         uncertain_threshold=0.15)
    s.indexer.sync()
    return s


def outbox(s) -> list[Path]:
    d = s.settings.outbox_dir
    return sorted(d.glob("*.eml")) if d.exists() else []


# ---------- (c) эскалация письмом ----------

def test_not_found_escalates_to_outbox_with_user_in_cc(svc):
    ans = svc.assistant.ask("Сколько суточных положено в командировке по России в выходной день?",
                            session_id="s1")
    assert ans["mode"] == "not_found"
    esc = ans["escalation"]
    assert esc and esc["to"] == QUALITY and esc["cc"] == USER and esc["delivery"] == "outbox"
    assert "запрос передан в службу качества" in ans["text"] and "вы в копии" in ans["text"]
    assert "Номер заявки: ГН-" in ans["text"] and "\nНомер заявки:" in ans["text"]
    files = outbox(svc)
    assert len(files) == 1
    msg = email.message_from_bytes(files[0].read_bytes(), policy=email.policy.default)
    assert msg["To"] == QUALITY and msg["Cc"] == USER
    assert f"[ГН-{esc['id']}]" in msg["Subject"]
    body = msg.get_body(("plain",)).get_content()
    assert "суточных" in body and "Причина" in body
    row = svc.db.query("SELECT outcome, escalated FROM answers WHERE answer_id=?", (ans["answer_id"],))[0]
    assert row["escalated"] == 1


def test_same_question_same_session_does_not_send_twice(svc):
    q = "Сколько суточных положено в командировке по России в выходной день?"
    a1 = svc.assistant.ask(q, session_id="s1")
    a2 = svc.assistant.ask(q, session_id="s1")
    assert a1["escalation"]["id"] == a2["escalation"]["id"] and a2["escalation"]["duplicate"]
    assert len(outbox(svc)) == 1


def test_smtp_is_used_only_when_configured(svc, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Cc"]))

    import app.escalation as esc_mod

    monkeypatch.setattr(esc_mod.smtplib, "SMTP", FakeSMTP)
    svc.settings.smtp_host = "smtp.test.invalid"
    svc.settings.smtp_user = "bot"
    ans = svc.assistant.ask("Как получить электронную подпись для УПД?", session_id="s2")
    assert ans["escalation"]["delivery"] == "smtp"
    assert ("send", QUALITY, USER) in sent and ("starttls",) in sent


def test_user_profile_email_goes_to_cc(svc):
    ans = svc.assistant.ask("Как подключиться к корпоративному VPN?", session_id="s3", user_email="me@example.local")
    assert ans["escalation"]["cc"] == "me@example.local"


def test_manual_escalation_endpoint(svc):
    app = create_app(services=svc)
    with TestClient(app) as c:
        a = c.post("/api/chat", json={"question": "Кто отвечает за договор с субподрядчиком?",
                                      "session_id": "web1", "division": "unknown"}).json()
        r = c.post("/api/escalate", json={"answer_id": a["answer_id"], "session_id": "web1"}).json()
        assert r["ok"] and r["escalation"]["reason"] == "manual" and "вы в копии" in r["text"]


# ---------- (d) дообучение через ответ эксперта ----------

Q_TRIP = "Сколько суточных положено сотруднику в командировке по России?"


def test_expert_answer_goes_to_faq_and_is_reused(svc):
    a1 = svc.assistant.ask(Q_TRIP, session_id="f1")
    esc_id = a1["escalation"]["id"]
    res = svc.escalations.answer(esc_id, "ОТВЕТ: 700 рублей за каждый день командировки, включая выходные.",
                                 expert="expert@example.local")
    assert res["final"] and res["faq_id"]
    a2 = svc.assistant.ask(Q_TRIP, session_id="f2")
    assert a2["mode"] == "faq" and "700 рублей" in a2["text"]
    assert a2["faq"]["label"] == "Разъяснение службы качества" and a2["faq"]["escalation_id"] == esc_id
    assert not a2.get("escalation")
    # регламенты не менялись
    assert svc.db.query("SELECT COUNT(*) n FROM files")[0]["n"] == 3


def test_expert_question_is_not_final(svc):
    a1 = svc.assistant.ask(Q_TRIP, session_id="f3")
    res = svc.escalations.answer(a1["escalation"]["id"], "ВОПРОС: уточните, о каком регионе речь?")
    assert res["final"] is False
    assert svc.assistant.ask(Q_TRIP, session_id="f4")["mode"] != "faq"


def test_import_eml_reply_from_expert(svc):
    a1 = svc.assistant.ask(Q_TRIP, session_id="e1")
    esc = a1["escalation"]
    reply = email.message.EmailMessage()
    reply["Subject"] = f"Re: [ГН-{esc['id']}] Вопрос без ответа в регламентах"
    reply["From"] = "Эксперт <expert@example.local>"
    reply["To"] = "gde-napisano@example.local"
    reply.set_content("700 рублей в сутки, выходные тоже оплачиваются.\n\n"
                      "-----Original Message-----\n> Сотрудник задал вопрос")
    res = svc.escalations.import_eml(reply.as_bytes())
    assert res["final"]
    faq = svc.faq.list()[0]
    assert faq["answer"].startswith("700 рублей") and "Original" not in faq["answer"]
    assert faq["expert"] == "expert@example.local"


def test_imap_poll_with_fake_server(svc):
    a1 = svc.assistant.ask(Q_TRIP, session_id="i1")
    reply = email.message.EmailMessage()
    reply["Subject"] = f"RE: [ГН-{a1['escalation']['id']}] вопрос"
    reply["From"] = "expert@example.local"
    reply.set_content("ОТВЕТ: 700 рублей.")

    class FakeIMAP:
        def login(self, u, p): pass
        def select(self, f): pass
        def search(self, *a): return "OK", [b"1"]
        def fetch(self, n, what): return "OK", [(b"1 (RFC822)", reply.as_bytes())]
        def logout(self): pass

    svc.settings.imap_host = "imap.test.invalid"
    out = svc.escalations.poll_imap(imap_factory=FakeIMAP)
    assert len(out["processed"]) == 1 and out["processed"][0]["final"]


def test_repeat_counter_suggests_regulation_review(svc):
    svc.settings.review_repeat_threshold = 2
    a1 = svc.assistant.ask(Q_TRIP, session_id="r1")
    svc.escalations.answer(a1["escalation"]["id"], "ОТВЕТ: 700 рублей.")
    svc.assistant.ask(Q_TRIP, session_id="r2")
    a3 = svc.assistant.ask(Q_TRIP, session_id="r3")
    assert a3["faq"]["review_suggested"] and any("пересмотреть регламент" in w for w in a3["warnings"])


def test_reply_parsing_helpers():
    from app.escalation import capitalize_first
    assert extract_reply("Ответ тут\n\n> цитата\n> ещё") == "Ответ тут"
    assert is_final_answer("ОТВЕТ: да") and not is_final_answer("Уточните регион?")
    assert not is_final_answer("А какой регион?")
    assert capitalize_first("корпоративной скидки нет") == "Корпоративной скидки нет"
    assert capitalize_first("700 рублей") == "700 рублей"


def test_expert_answer_capitalizes_after_otvet_prefix(svc):
    a1 = svc.assistant.ask("Какой размер корпоративной скидки на парковку в офисе?", session_id="cap1")
    assert a1.get("escalation")
    res = svc.escalations.answer(a1["escalation"]["id"],
                                 "ОТВЕТ: корпоративной скидки в этом офисе нет.", expert="qa@example.local")
    assert res["final"]
    item = next(x for x in svc.faq.list() if x["id"] == res["faq_id"])
    assert item["answer"].startswith("Корпоративной скидки")
    again = svc.assistant.ask("Какой размер корпоративной скидки на парковку в офисе?", session_id="cap2")
    assert again["mode"] == "faq" and again["text"].startswith("Корпоративной скидки")


# ---------- (b) уточнение подразделения ----------

Q_INIT = "За сколько нужно инициировать заявку на расчёт до подачи предложения?"


def test_divergent_divisions_ask_to_clarify(svc):
    ans = svc.assistant.ask(Q_INIT, session_id="d1")
    assert ans["mode"] == "clarify"
    ids = [o["id"] for o in ans["clarify"]["options"]]
    assert {"dbrct", "tech"} <= set(ids) and ids[-1] == "unknown"
    assert not ans.get("escalation")


def test_division_answer_is_strict_and_remembered(svc):
    svc.assistant.ask(Q_INIT, session_id="d2")
    ans = svc.assistant.ask(Q_INIT, session_id="d2", division="tech")
    assert ans["mode"] == "confident"
    assert {c["box"] for c in ans["citations"]} == {"tech"}
    assert "2 недели" in ans["citations"][0]["quote"]
    # подразделение запомнено в сессии: следующий вопрос без уточнения
    nxt = svc.assistant.ask("Кто отвечает за договор с субподрядчиком?", session_id="d2")
    assert nxt["mode"] == "confident" and {c["box"] for c in nxt["citations"]} == {"tech"}
    assert nxt["profile"]["division"] == "tech"


def test_unknown_division_shows_all_variants_separately(svc):
    ans = svc.assistant.ask(Q_INIT, session_id="d3", division="unknown")
    assert ans["mode"] == "by_division"
    secs = {s["division"]: s for s in ans["sections"]}
    assert "3 рабочих дня" in json.dumps(secs["dbrct"], ensure_ascii=False)
    assert "2 недели" in json.dumps(secs["tech"], ensure_ascii=False)
    # сроки не усредняются: в разделе ДБРЦТ нет срока техдирекции и наоборот
    assert "2 недели" not in json.dumps(secs["dbrct"], ensure_ascii=False)
    assert "3 рабочих" not in json.dumps(secs["tech"], ensure_ascii=False)


def test_explicit_division_in_question_skips_clarify(svc):
    ans = svc.assistant.ask("За сколько в ДБРЦТ инициировать заявку на расчёт?", session_id="d4")
    assert ans["mode"] == "confident" and ans["division"]["id"] == "dbrct"


def test_two_divisions_named_answered_separately(svc):
    ans = svc.assistant.ask("Назови сроки инициации заявки на расчёт в Технической дирекции и в ДБРЦТ", session_id="d5")
    assert ans["mode"] == "by_division" and [s["division"] for s in ans["sections"]] == ["tech", "dbrct"]


def test_clarify_limit_then_all_variants(svc):
    svc.settings.clarify_max = 1
    assert svc.assistant.ask(Q_INIT, session_id="d6")["mode"] == "clarify"
    assert svc.assistant.ask(Q_INIT, session_id="d6")["mode"] == "by_division"


def test_compare_question_goes_straight_to_variants(svc):
    ans = svc.assistant.ask("Можно ли взять средний срок инициации заявки на расчёт для всех подразделений?",
                            session_id="d7")
    assert ans["mode"] == "by_division"


# ---------- (a) стиль: выжимка + ссылка на каждый пункт ----------

def test_answer_has_links_to_highlighted_place(svc):
    ans = svc.assistant.ask("В какой срок в ДБРЦТ руководитель проекта создаёт смету после подписания договора?",
                            session_id="a1")
    assert ans["mode"] == "confident" and ans["citations"]
    c = ans["citations"][0]
    assert c["clause_no"] == "10.2" and c["link"] == f"/doc/{c['doc_id']}?p={c['point_id']}"
    app = create_app(services=svc)
    with TestClient(app) as client:
        doc = client.get(f"/api/documents/{c['doc_id']}/clauses").json()
        assert any(x["point_id"] == c["point_id"] for x in doc["clauses"])
        assert client.get(c["link"]).status_code == 200


def test_prompt_asks_for_plain_language_summary():
    from app.llm.prompts import ANSWER_SYSTEM

    assert "человеческим языком" in ANSWER_SYSTEM and "conflict" in ANSWER_SYSTEM
    assert "не усредняй" in ANSWER_SYSTEM


def test_number_self_check_still_works(services_factory, svc):
    from app.llm.fake import FakeLLM

    svc.answers.llm = FakeLLM("wrong_number")
    ans = svc.assistant.ask("В какой срок в ДБРЦТ руководитель проекта создаёт смету после подписания договора?",
                            session_id="a2")
    assert ans["answer_kind"] == "quote_only" and "999" not in ans["text"]


def test_llm_conflict_flag_escalates(svc):
    from app.llm.base import LLM, LLMResult

    class ConflictLLM(LLM):
        provider = "stub"

        def chat(self, system, user, json_mode=False):
            import re as _re

            src = _re.search(r"<<<ПУНКТ 1>>>\n(.+?)\n<<<КОНЕЦ", user, _re.S).group(1)
            first = src.split(".")[0] + "."
            return LLMResult(json.dumps({"answerable": True, "answer": f"Пункты расходятся. {first} [1]",
                                         "used": [1], "quotes": [{"n": 1, "quote": first}], "advice": "",
                                         "conflict": True}, ensure_ascii=False), 10, 10, "stub", "stub")

    svc.answers.llm = ConflictLLM()
    ans = svc.assistant.ask("Гарантийный срок исполнителя в Дирекции ИБ?", session_id="c1")
    assert ans["conflict"] and ans["escalation"]["reason"] == "conflict"
    assert ans["notice"].startswith("Пункты регламента расходятся")
    assert "однозначно ответить нельзя" in ans["notice"] and "Вопрос передан в службу качества" in ans["notice"]
    assert "Номер заявки: ГН-" in ans["notice"] and "\nНомер заявки:" in ans["notice"]
    assert "Что написано в документах" not in ans["notice"]
    assert "передал" not in ans["notice"]
    assert "Пункты расходятся" in ans["text"]


# ---------- (e) вопросы вне базы и отсутствующие документы ----------

@pytest.mark.parametrize("q,kind", [
    ("Я закрыл сделку такому-то клиенту. Какая у меня будет маржа или премия?", "money"),
    ("Сделай follow-up по встрече с заказчиком: краткое письмо и следующие шаги.", "generate"),
    ("Сделай анализ прошедшей встречи с заказчиком", "generate"),
    ("Какой сегодня курс доллара?", "smalltalk"),
])
def test_off_topic_refused_without_search(svc, q, kind):
    assert off_topic(q).kind == kind
    ans = svc.assistant.ask(q, session_id="o1")
    assert ans["mode"] == "offtopic" and not ans["citations"] and not ans.get("escalation")
    if kind == "money":
        assert "руководител" in ans["text"]


def test_regular_questions_are_not_off_topic():
    for q in ("Кто отвечает за договор с субподрядчиком?", "Можно ли начать работы без договора и сметы?",
              "Напиши, где сказано про смету"):
        assert off_topic(q) is None


def test_missing_regulation_named_not_invented(svc):
    ans = svc.assistant.ask("Как в ДБРЦТ ищут субподрядчика? Опиши процедуру по регламенту подбора субподрядчика.",
                            session_id="m1")
    assert any("подбора субподрядчика" in n for n in ans["missing_docs"])
    assert any("нет в выданной базе" in w for w in ans["warnings"])


def test_missing_documents_detection():
    titles = ["Проектная деятельность ДБРЦТ", "Пресейловая деятельность Дирекции ИБ"]
    assert missing_documents("по регламенту проектной деятельности ДБРЦТ", titles) == []
    assert missing_documents("согласно Регламенту привлечения подрядчиков на коммерческие проекты", titles,
                             strict=True) == ["Регламент привлечения подрядчиков на коммерческие проекты"]


# ---------- (f) глоссарий ----------

def test_glossary_expands_query_and_explains_terms():
    g = Glossary.load(ROOT / "examples" / "glossary.tsv")
    q, terms = g.expand("Что такое ЗнП и кто её создаёт?")
    assert "Заявка на пресейл" in q and terms[0].abbr == "ЗНП"
    q2, _ = g.expand("Что делает руководитель проекта?")
    assert "РП" in q2
    # «по» — предлог, а не «ПО» (программное обеспечение)
    assert [t.abbr for t in g.find("работа по договору")] == []
    assert {"abbr": "РП", "full": "Руководитель проекта"} in g.explain("РП отвечает")


def test_glossary_in_answer(svc):
    ans = svc.assistant.ask("Кто в Дирекции ИБ отвечает за договор с субподрядчиком, РП?", session_id="g1")
    assert {"abbr": "РП", "full": "Руководитель проекта"} in ans["glossary"]


# ---------- (g) лог действий и аналитика ----------

def test_analytics_screen(svc):
    svc.settings.llm_price_per_1k_tokens = 0.2
    svc.settings.admin_token = "a" * 24
    svc.assistant.ask(Q_TRIP, session_id="x1")
    svc.assistant.ask(Q_TRIP, session_id="x2")
    svc.assistant.ask("Сделай анализ встречи", session_id="x3")
    svc.assistant.ask("За сколько в ДБРЦТ инициировать заявку на расчёт?", session_id="x4")
    app = create_app(services=svc)
    with TestClient(app) as c:
        assert c.get("/api/admin/analytics").status_code == 401
        data = c.get("/api/admin/analytics", headers={"X-Admin-Token": "a" * 24}).json()
        assert data["by_outcome"]["not_found"] == 2 and data["by_outcome"]["offtopic"] == 1
        assert data["escalations"]["total"] == 2
        assert data["repeats"][0]["count"] == 2
        assert data["popular_docs"][0]["doc_title"].startswith("Проектная деятельность ДБРЦТ")
        assert data["latency_ms"]["p95"] >= 0 and data["cost"]["avg_tokens"] > 0
        esc = c.get("/api/operator/escalations", headers={"X-Admin-Token": "a" * 24}).json()
        eml = c.get(f"/api/operator/outbox/{esc[0]['eml']}", headers={"X-Admin-Token": "a" * 24})
        assert eml.status_code == 200 and b"Subject:" in eml.content
        r = c.post(f"/api/operator/escalations/{esc[0]['id']}/answer", headers={"X-Admin-Token": "a" * 24},
                   json={"text": "ОТВЕТ: 700 рублей.", "expert": "qa"}).json()
        assert r["final"]
        again = c.post("/api/chat", json={"question": Q_TRIP, "session_id": "x5"}).json()
        assert again["mode"] == "faq"


def test_outbox_name_is_validated(svc):
    svc.settings.admin_token = "a" * 24
    app = create_app(services=svc)
    with TestClient(app) as c:
        assert c.get("/api/operator/outbox/..%2F..%2Fsecret", headers={"X-Admin-Token": "a" * 24}).status_code == 404


# ---------- предохранитель LLM и запасной путь по оценке реранкера ----------

def test_circuit_breaker_stops_waiting_on_dead_llm():
    from app.llm import GuardedLLM, LLMError
    from app.llm.base import LLM

    class Dead(LLM):
        provider, model = "stub", "stub"
        calls = 0

        def chat(self, system, user, json_mode=False):
            Dead.calls += 1
            raise LLMError("down")

    now = [0.0]
    g = GuardedLLM(Dead(), failures=2, cooldown=30, clock=lambda: now[0])
    for _ in range(2):
        with pytest.raises(LLMError):
            g.chat("s", "u")
    assert Dead.calls == 2 and g.is_open and not g.healthy()
    with pytest.raises(LLMError, match="предохранитель"):
        g.chat("s", "u")
    assert Dead.calls == 2  # внутрь не ходили
    now[0] = 31.0
    with pytest.raises(LLMError):
        g.chat("s", "u")
    assert Dead.calls == 3  # после паузы одна проба


def test_circuit_breaker_closes_on_success_and_keeps_config_errors():
    from app.llm import GuardedLLM, LLMConfigError, LLMError, LLMResult
    from app.llm.base import LLM

    class Flaky(LLM):
        provider, model = "stub", "stub"
        queue = [LLMError("x"), LLMResult("ok")]

        def chat(self, system, user, json_mode=False):
            r = Flaky.queue.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    g = GuardedLLM(Flaky(), failures=2, cooldown=30)
    with pytest.raises(LLMError):
        g.chat("s", "u")
    assert g.chat("s", "u").text == "ok" and not g.is_open

    class NoKey(LLM):
        provider, model = "stub", "stub"

        def chat(self, system, user, json_mode=False):
            raise LLMConfigError("нет ключа")

    g2 = GuardedLLM(NoKey(), failures=1)
    with pytest.raises(LLMConfigError):
        g2.chat("s", "u")
    assert not g2.is_open


def test_make_llm_wraps_real_providers_only(tmp_path):
    from app.llm import GuardedLLM, make_llm
    from tests.conftest import make_settings

    assert isinstance(make_llm(make_settings(tmp_path, llm_provider="ollama")), GuardedLLM)
    assert not isinstance(make_llm(make_settings(tmp_path, llm_provider="fake")), GuardedLLM)
    assert not isinstance(make_llm(make_settings(tmp_path, llm_provider="ollama", llm_breaker_failures=0)), GuardedLLM)


def test_reranker_fallback_shows_clause_when_model_says_no(svc):
    from app.llm.base import LLM, LLMResult

    class SaysNo(LLM):
        provider, model = "stub", "stub"

        def chat(self, system, user, json_mode=False):
            return LLMResult(json.dumps({"answerable": False, "answer": ""}), 5, 5, "stub", "stub")

    svc.answers.llm = SaysNo()
    svc.settings.quote_fallback_score = 0.0
    ans = svc.assistant.ask("В какой срок в ДБРЦТ руководитель проекта создаёт смету после подписания договора?",
                            session_id="q1")
    assert ans["mode"] == "confident" and ans["answer_kind"] == "quote_only"
    assert ans["citations"][0]["clause_no"] == "10.2" and not ans.get("escalation")
    svc.settings.quote_fallback_score = 1.01  # выключено: прежнее поведение
    off = svc.assistant.ask("В какой срок в ДБРЦТ руководитель проекта создаёт смету после подписания договора?",
                            session_id="q2")
    assert off["mode"] in ("uncertain", "not_found") and off["escalation"]
