"""Защита стенда: адрес клиента за прокси, лимиты, блокировка подбора токена, заголовки, отзывы, GigaChat, прогрев."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.security import AdminGuard, ip_allowed
from tests.test_answers import _load

TOKEN = "admin-token-for-tests-1234"
Q = "Сколько календарных дней ежегодного оплачиваемого отпуска?"


def client(svc, ip="203.0.113.5"):
    return TestClient(create_app(services=svc), client=(ip, 50000))


# ---------- заголовки ----------

def test_security_headers_on_pages_and_api(services_factory):
    with client(services_factory()) as c:
        for path in ("/", "/api/health", "/admin"):
            h = c.get(path).headers
            assert "frame-ancestors 'none'" in h["content-security-policy"]
            assert "unsafe-inline" not in h["content-security-policy"]
            assert h["x-frame-options"] == "DENY"
            assert h["x-content-type-options"] == "nosniff"
            assert h["referrer-policy"] == "no-referrer"
        assert c.get("/admin").headers["cache-control"] == "no-store"
        assert c.get("/api/admin/stats").headers["cache-control"] == "no-store"
        # Swagger работает: ему разрешён CDN
        assert "cdn.jsdelivr.net" in c.get("/docs").headers["content-security-policy"]


# ---------- админка ----------

def test_admin_lockout_after_failed_tokens(services_factory):
    svc = services_factory(admin_token=TOKEN, admin_max_failures=3, admin_lockout_seconds=60)
    with client(svc) as c:
        for _ in range(3):
            assert c.get("/api/admin/stats", headers={"X-Admin-Token": "guess"}).status_code == 401
        r = c.get("/api/admin/stats", headers={"X-Admin-Token": TOKEN})
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # другой адрес не заблокирован
    with client(svc, ip="198.51.100.7") as c2:
        assert c2.get("/api/admin/stats", headers={"X-Admin-Token": TOKEN}).status_code == 200


def test_admin_guard_doubles_lock_and_resets_on_success():
    now = [0.0]
    g = AdminGuard(max_failures=2, lockout_seconds=10, clock=lambda: now[0])
    assert g.failure("1.1.1.1") == 0 and g.failure("1.1.1.1") == 10
    assert g.locked_for("1.1.1.1") > 0
    now[0] = 11
    assert g.locked_for("1.1.1.1") == 0
    g.failure("1.1.1.1")
    assert g.failure("1.1.1.1") == 20  # вторая блокировка вдвое длиннее
    now[0] = 100
    g.failure("2.2.2.2")
    g.success("2.2.2.2")
    assert g.failure("2.2.2.2") == 0  # после удачного входа счётчик сброшен


def test_admin_short_token_is_rejected(services_factory):
    with client(services_factory(admin_token="short")) as c:
        r = c.get("/api/admin/stats", headers={"X-Admin-Token": "short"})
        assert r.status_code == 403 and "короче" in r.json()["detail"]


def test_admin_allowed_ips(services_factory):
    svc = services_factory(admin_token=TOKEN, admin_allowed_ips="10.0.0.0/8, 192.0.2.1")
    with client(svc, ip="203.0.113.5") as c:
        assert c.get("/api/admin/stats", headers={"X-Admin-Token": TOKEN}).status_code == 403
    with client(svc, ip="10.20.30.40") as c:
        assert c.get("/api/admin/stats", headers={"X-Admin-Token": TOKEN}).status_code == 200
    assert ip_allowed("192.0.2.1", svc.settings.admin_allowed_networks)
    assert not ip_allowed("not-an-ip", svc.settings.admin_allowed_networks)
    assert ip_allowed("anything", [])


def test_bad_admin_allowed_ips_fail_at_start(tmp_path):
    from pydantic import ValidationError

    from tests.conftest import make_settings

    with pytest.raises((ValidationError, ValueError)):
        make_settings(tmp_path, admin_allowed_ips="10.0.0.0/33")


def test_upload_rate_limit_and_replace_warning(services_factory):
    svc = services_factory(admin_token=TOKEN, upload_rate_limit_per_minute=3)
    h = {"X-Admin-Token": TOKEN}
    with client(svc) as c:
        f = {"file": ("Памятка.txt", "1.1. Отпуск 28 дней.".encode())}
        assert c.post("/api/admin/upload", headers=h, data={"box": "hr"}, files=f).status_code == 200
        f = {"file": ("Памятка.txt", "1.1. Отпуск 30 дней.".encode())}
        r = c.post("/api/admin/upload", headers=h, data={"box": "hr"}, files=f)
        assert r.status_code == 409 and "редакция от" in r.json()["detail"]
        r = c.post("/api/admin/upload", headers=h, data={"box": "hr", "replace": "true"}, files=f)
        assert r.status_code == 200 and r.json()["replaced"] is True and r.json()["warning"]
        assert "30 дней" in (svc.indexer.docs_dir / "hr" / "Памятка.txt").read_text(encoding="utf-8")
        r = c.post("/api/admin/upload", headers=h, data={"box": "hr"}, files=f)
        assert r.status_code == 429


# ---------- адрес клиента и лимиты ----------

def test_chat_limit_is_per_client_ip(services_factory):
    svc = services_factory(rate_limit_per_minute=2)
    _load(svc)
    with client(svc, ip="203.0.113.10") as a:
        assert a.post("/api/chat", json={"question": Q}).status_code == 200
        assert a.post("/api/chat", json={"question": Q}).status_code == 200
        assert a.post("/api/chat", json={"question": Q}).status_code == 429
    with client(svc, ip="203.0.113.11") as b:
        assert b.post("/api/chat", json={"question": Q}).status_code == 200


def test_trusted_bot_ip_gets_its_own_limit(services_factory):
    svc = services_factory(rate_limit_per_minute=1, trusted_client_ips="172.28.0.11", rate_limit_trusted_per_minute=5)
    _load(svc)
    with client(svc, ip="172.28.0.11") as bot:
        assert all(bot.post("/api/chat", json={"question": Q}).status_code == 200 for _ in range(5))
        assert bot.post("/api/chat", json={"question": Q}).status_code == 429


def test_proxy_headers_trusted_only_from_proxy(services_factory):
    """Как в контейнере: uvicorn --proxy-headers с FORWARDED_ALLOW_IPS=адрес Caddy."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    svc = services_factory(rate_limit_per_minute=1)
    _load(svc)
    app = create_app(services=svc)
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="172.28.0.10")
    with TestClient(app):  # запускаем lifespan
        via_proxy = TestClient(wrapped, client=("172.28.0.10", 1))
        # разные настоящие клиенты за прокси — разные счётчики
        assert via_proxy.post("/api/chat", json={"question": Q}, headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200
        assert via_proxy.post("/api/chat", json={"question": Q}, headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 200
        assert via_proxy.post("/api/chat", json={"question": Q}, headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 429
        # не с прокси: подделанный X-Forwarded-For не помогает обойти лимит
        direct = TestClient(wrapped, client=("203.0.113.99", 1))
        assert direct.post("/api/chat", json={"question": Q}, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
        assert direct.post("/api/chat", json={"question": Q}, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


# ---------- отзывы и практика ----------

def _point(ans):
    return ans["citations"][0]["point_id"] if ans["citations"] else ans["variants"][0]["point_id"]


def test_one_vote_per_answer_and_point(services_factory):
    svc = services_factory()
    _load(svc)
    with client(svc) as c:
        ans = c.post("/api/chat", json={"question": Q}).json()
        for helpful in (True, True, True, False):
            assert c.post("/api/feedback", json={"answer_id": ans["answer_id"], "helpful": helpful,
                                                 "point_id": _point(ans)}).status_code == 200
    rows = svc.db.query("SELECT helpful, client FROM feedback WHERE answer_id=?", (ans["answer_id"],))
    assert len(rows) == 1 and rows[0]["helpful"] == 0  # последний голос заменил прежние
    assert rows[0]["client"] and "203.0.113.5" not in rows[0]["client"]  # адрес не хранится


def test_feedback_from_one_client_cannot_promote_irrelevant_point(services_factory):
    """Десятки «да» с одного адреса весят как один голос, а подъём оценки ограничен MEMORY_MAX_BOOST."""
    svc = services_factory(confident_threshold=0.95, memory_boost=1.0, memory_max_boost=0.15,
                           feedback_rate_limit_per_minute=0)
    _load(svc)
    q = "пароль от почты отпуск"
    first = svc.answers.ask(q)
    target = min(first["variants"], key=lambda v: v["score"])
    before = target["score"]
    with client(svc) as c:
        for _ in range(30):
            a = c.post("/api/chat", json={"question": q}).json()
            pid = next((v["point_id"] for v in a["variants"] if v["clause_no"] == target["clause_no"]
                        and v["doc_id"] == target["doc_id"]), None)
            if pid:
                c.post("/api/feedback", json={"answer_id": a["answer_id"], "helpful": True, "point_id": pid})
    after = next(c for c in svc.searcher.search(q).candidates
                 if c.doc_id == target["doc_id"] and c.clause_no == target["clause_no"])
    assert after.score <= before + 0.15 + 1e-6
    assert after.score > before  # но отзыв всё же учтён


def test_feedback_and_practice_rate_limits(services_factory):
    svc = services_factory(feedback_rate_limit_per_minute=2, practice_rate_limit_per_minute=1)
    _load(svc)
    with client(svc) as c:
        ans = c.post("/api/chat", json={"question": Q}).json()
        codes = [c.post("/api/feedback", json={"answer_id": ans["answer_id"], "helpful": True}).status_code
                 for _ in range(3)]
        assert codes == [200, 200, 429]
        body = {"answer_id": ans["answer_id"], "text": "Оформили через кадры"}
        assert c.post("/api/practice", json=body).status_code == 200
        assert c.post("/api/practice", json=body).status_code == 429


def test_practice_hidden_until_approved_by_default(services_factory):
    svc = services_factory()
    assert svc.settings.practice_requires_approval is True
    _load(svc)
    with client(svc) as c:
        ans = c.post("/api/chat", json={"question": Q}).json()
        c.post("/api/practice", json={"answer_id": ans["answer_id"], "text": "Реклама: купите курс",
                                      "point_id": ans["citations"][0]["point_id"]})
        again = c.post("/api/chat", json={"question": Q}).json()
        assert not any("Реклама" in p["text"] for p in again["practice"])


def test_old_database_is_migrated(tmp_path):
    """База прежней версии: без колонки client и с повторными голосами."""
    from app.db import SCHEMA, Database

    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for _ in range(3):
        con.execute("INSERT INTO feedback(answer_id, question, doc_id, clause_no, helpful) VALUES('a','q','d','1',1)")
    con.commit()
    con.close()
    db = Database(path)
    rows = db.query("SELECT * FROM feedback")
    assert len(rows) == 1 and "client" in rows[0].keys()
    db.close()


# ---------- прогрев ----------

def test_ready_waits_for_model_warmup(services_factory):
    svc = services_factory()
    app = create_app(services=svc)
    with TestClient(app) as c:
        assert c.get("/ready").json()["checks"]["models"] is True
        app.state.warm["done"] = False  # как будто реранкер ещё грузится
        r = c.get("/ready")
        assert r.status_code == 503 and r.json()["checks"]["models"] is False


def test_warmup_failure_does_not_break_start(services_factory):
    from app.main import _warmup

    svc = services_factory()
    app = create_app(services=svc)
    with TestClient(app):
        class Broken:
            def score(self, *a):
                raise RuntimeError("нет памяти")

        svc.reranker = Broken()
        app.state.warm = {"done": False}
        _warmup(app)
        assert app.state.warm["done"] is True and app.state.warm["error"] == "RuntimeError"


# ---------- GigaChat ----------

class _Msg:
    def __init__(self, text):
        self.message = type("M", (), {"content": text})()


class _Resp:
    def __init__(self, text="{}"):
        self.choices = [_Msg(text)]
        self.usage = type("U", (), {"prompt_tokens": 3, "completion_tokens": 2})()


def test_gigachat_reuses_one_client_and_retries(tmp_path):
    pytest.importorskip("gigachat")
    from gigachat.exceptions import AuthenticationError, RateLimitError

    from app.llm import LLMError
    from app.llm.gigachat import GigaChatLLM
    from tests.conftest import make_settings

    made, plan = [], []

    class Client:
        def chat(self, payload):
            step = plan.pop(0)
            if isinstance(step, Exception):
                raise step
            return _Resp(step)

    def factory():
        made.append(1)
        return Client()

    s = make_settings(tmp_path, llm_provider="gigachat", gigachat_credentials="cred", llm_retries=2)
    sleeps = []
    llm = GigaChatLLM(s, client_factory=factory, sleep=sleeps.append)
    plan[:] = [RateLimitError("https://x", 429, b"slow down", None), '{"ok": 1}', '{"ok": 2}']
    assert llm.chat("s", "u").text == '{"ok": 1}'
    assert llm.chat("s", "u").text == '{"ok": 2}'
    assert len(made) == 1 and sleeps == [1.0]  # один клиент, одна пауза перед повтором
    plan[:] = [AuthenticationError("https://x", 401, b'{"message":"bad key secret-body"}', None)]
    with pytest.raises(LLMError) as e:
        llm.chat("s", "u")
    assert "401" in str(e.value) and "secret-body" not in str(e.value)  # тело ответа в текст ошибки не попадает
    assert plan == []  # 401 не повторяется


def test_gigachat_ca_bundle_autodetect(tmp_path, monkeypatch):
    from app.llm import LLMConfigError
    from app.llm import gigachat as g

    pem = tmp_path / "russian_trusted_root_ca.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(g, "DEFAULT_CA_PATHS", (str(pem),))
    assert g.find_ca_bundle("") == str(pem)
    assert g.find_ca_bundle(str(pem)) == str(pem)
    with pytest.raises(LLMConfigError):
        g.find_ca_bundle(str(tmp_path / "missing.pem"))
    monkeypatch.setattr(g, "DEFAULT_CA_PATHS", (str(tmp_path / "nope.pem"),))
    assert g.find_ca_bundle("") is None


def test_ready_waits_for_startup_index(services_factory):
    """После восстановления из копии индекс пересобирается: пока он не готов, /ready отвечает 503."""
    import threading

    svc = services_factory(index_on_start=True)
    gate = threading.Event()
    real = svc.indexer.sync

    def slow():
        gate.wait(5)
        return real()

    svc.indexer.sync = slow
    app = create_app(services=svc)
    with TestClient(app) as c:
        r = c.get("/ready")
        assert r.status_code == 503 and r.json()["checks"]["index"] is False
        gate.set()
        for _ in range(100):
            if c.get("/ready").status_code == 200:
                break
            import time
            time.sleep(0.05)
        assert c.get("/ready").json()["checks"]["index"] is True
