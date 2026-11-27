"""Публичная витрина хранилища: список регламентов и разъяснений службы качества."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _index(svc):
    docs = svc.settings.docs_dir
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "otpusk.txt").write_text(
        "# 3. Отпуск\n3.1 Ежегодный отпуск составляет 28 календарных дней.\n", encoding="utf-8")
    (docs / "pass.txt").write_text(
        "# 2. Пароль\n2.1 Пароль меняется каждые 90 дней.\n", encoding="utf-8")
    svc.indexer.sync()
    svc.db.execute(
        "INSERT INTO files(doc_id, rel_path, title, box, sha256, size, mtime, n_clauses, status, error) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("broken-doc", "broken.txt", "Неразобранный файл", "hr", "0", 1, 0, 0, "error", "сбой разбора"))
    fid = svc.faq.add("Сколько суточных в выходной?", "700 рублей за сутки.",
                      expert="expert@example.local", escalation_id=7)
    hidden = svc.faq.add("Скрытый вопрос", "Этот ответ не должен быть на витрине.",
                         expert="expert@example.local")
    svc.db.execute("UPDATE faq SET active=0 WHERE id=?", (hidden,))
    return fid


def test_wiki_page_and_index_are_public(services_factory):
    svc = services_factory(admin_token="s3cret-token-long-enough")
    fid = _index(svc)
    with TestClient(create_app(services=svc)) as c:
        page = c.get("/wiki")
        assert page.status_code == 200
        html = page.text
        assert "Хранилище" in html and "/static/wiki.js" in html
        assert "Разъяснения службы качества" in html
        home = c.get("/")
        assert home.status_code == 200 and 'href="/wiki"' in home.text
        r = c.get("/api/wiki")
        assert r.status_code == 200
        data = r.json()
        titles = {d["title"] for d in data["documents"]}
        assert len(data["documents"]) >= 2
        assert "Неразобранный файл" not in titles
        assert all(d["n_clauses"] > 0 and d["doc_id"] and d["title"] for d in data["documents"])
        assert all("rel_path" not in d and "error" not in d and "sha256" not in d for d in data["documents"])
        assert all(d.get("doc_date") is None or isinstance(d.get("doc_date"), str) for d in data["documents"])
        for d in data["documents"]:
            doc = c.get(f"/api/documents/{d['doc_id']}/clauses")
            assert doc.status_code == 200
            body = doc.json()
            assert body["clauses"] and all("point_id" in x for x in body["clauses"])
            assert c.get(f"/doc/{d['doc_id']}").status_code == 200
        faq_ids = {f["id"] for f in data["faq"]}
        assert fid in faq_ids
        item = next(f for f in data["faq"] if f["id"] == fid)
        assert item["question"] == "Сколько суточных в выходной?"
        assert item["answer"] == "700 рублей за сутки."
        assert item["escalation_id"] == 7
        assert item["created_at"]
        assert "expert" not in item and "hits" not in item
        assert "Скрытый вопрос" not in {f["question"] for f in data["faq"]}
        assert c.get("/api/admin/documents").status_code in (401, 403)


def test_wiki_empty_lists(services_factory):
    svc = services_factory()
    with TestClient(create_app(services=svc)) as c:
        data = c.get("/api/wiki").json()
        assert data["documents"] == [] and data["faq"] == []
