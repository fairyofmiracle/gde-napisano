"""Демо-ящик службы качества: письма из outbox, прочитано/не прочитано, ссылки на пункты, доступ по токену."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_answers import _load

TOKEN = "t" * 24
H = {"X-Admin-Token": TOKEN}


@pytest.fixture
def client(services_factory):
    svc = services_factory(admin_token=TOKEN)
    _load(svc)
    with TestClient(create_app(services=svc)) as c:
        yield c, svc


def _escalate(svc, sid="m1"):
    doc = svc.db.query("SELECT doc_id, title FROM files ORDER BY title LIMIT 1")[0]
    cl = svc.db.query("SELECT clause_no, text FROM clauses WHERE doc_id=? AND clause_no!='' ORDER BY ord LIMIT 1",
                      (doc["doc_id"],))[0]
    found = [{"ref": f"{doc['title']}, п. {cl['clause_no']}", "text": cl["text"], "doc_id": doc["doc_id"],
              "doc_title": doc["title"], "clause_no": cl["clause_no"]}]
    return svc.escalations.create("Как оформить отпуск на Луне?", "not_found", found, session_id=sid), doc


def test_mail_needs_token(client):
    c, _ = client
    assert c.get("/api/operator/mail").status_code in (401, 403)
    assert c.get("/mail").status_code == 200  # сама страница открыта, данные только с токеном


def test_list_open_read_unread(client):
    c, svc = client
    esc, doc = _escalate(svc)
    assert esc["delivery"] == "outbox"  # наружу ничего не уходит
    items = c.get("/api/operator/mail", headers=H).json()
    m = next(i for i in items if i["id"] == esc["id"])
    assert m["ticket"] == f"ГН-{esc['id']}" and m["subject"].startswith(f"[ГН-{esc['id']}]")
    assert m["read"] is False and m["has_eml"] and m["date"]
    full = c.get(f"/api/operator/mail/{esc['id']}", headers=H).json()
    assert full["question"] == "Как оформить отпуск на Луне?" and "Здравствуйте" in full["body"]
    assert full["found"][0]["link"].startswith(f"/doc/{doc['doc_id']}?p=")
    assert full["reply_url"] == f"/admin?esc={esc['id']}#quality"
    assert next(i for i in c.get("/api/operator/mail", headers=H).json() if i["id"] == esc["id"])["read"] is True
    assert c.post(f"/api/operator/mail/{esc['id']}/read?read=false", headers=H).json()["ok"]
    assert next(i for i in c.get("/api/operator/mail", headers=H).json() if i["id"] == esc["id"])["read"] is False
    assert c.get("/api/operator/mail/99999", headers=H).status_code == 404
    assert c.post("/api/operator/mail/99999/read", headers=H).status_code == 404


def test_missing_eml_still_listed(client):
    c, svc = client
    esc, _ = _escalate(svc, "m2")
    svc.db.execute("UPDATE escalations SET eml_path='../../etc/passwd' WHERE id=?", (esc["id"],))
    m = next(i for i in c.get("/api/operator/mail", headers=H).json() if i["id"] == esc["id"])
    assert m["has_eml"] is False and m["subject"].startswith(f"[ГН-{esc['id']}]")
    full = c.get(f"/api/operator/mail/{esc['id']}", headers=H).json()
    assert full["body"] == ""


def test_page_marks_demo_and_nav_links(client):
    c, _ = client
    page = c.get("/mail").text
    assert "Демо-ящик: письма наружу не отправляются" in page
    assert 'id="mail-link" href="/mail" hidden' in c.get("/").text
    assert 'href="/mail"' in c.get("/admin").text
