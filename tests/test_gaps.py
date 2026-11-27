"""Отчёт «Где не написано?»: публичный, без почты, собирается из заявок и корпуса."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import gaps as gaps_mod
from app.main import create_app
from tests.test_answers import _load


def _seed(svc):
    _load(svc)
    docs = list(svc.db.query("SELECT doc_id, title FROM files WHERE status='ok'"))
    holiday = next(d for d in docs if "отпуск" in d["title"].lower())
    # пункт со ссылкой на приказ, которого нет в базе, и на отмену
    rows = svc.db.query("SELECT point_id, clause_no, text FROM clauses WHERE doc_id=? LIMIT 1", (holiday["doc_id"],))
    point = rows[0]
    svc.db.execute(
        "UPDATE clauses SET text=? WHERE point_id=?",
        (point["text"] + " Порядок согласован с приказом № 17-ОД от 01.02.2024. "
         "Регламент командировок утратил силу.", point["point_id"]),
    )
    found_conflict = json.dumps([
        {"doc_id": holiday["doc_id"], "doc_title": holiday["title"], "clause_no": "5.2.2",
         "point_id": point["point_id"], "ref": f"{holiday['title']}, п. 5.2.2",
         "text": "Приоритет не меняется, если не выдержан SLA."},
        {"doc_id": holiday["doc_id"], "doc_title": holiday["title"], "clause_no": "5.2.3",
         "point_id": point["point_id"], "ref": f"{holiday['title']}, п. 5.2.3",
         "text": "ПА может самостоятельно понизить приоритет."},
    ], ensure_ascii=False)
    found_nf = json.dumps([
        {"doc_id": holiday["doc_id"], "doc_title": holiday["title"], "clause_no": "3.1",
         "point_id": point["point_id"], "ref": f"{holiday['title']}, п. 3.1",
         "text": "Отпуск составляет 28 календарных дней."},
    ], ensure_ascii=False)
    svc.db.execute(
        "INSERT INTO escalations(question, norm_question, reason, division, found, user_email, user_name, "
        "to_email, status) VALUES(?,?,?,?,?,?,?,?,?)",
        ("Можно ли парковаться на территории офиса в Казани?",
         "можно ли парковаться на территории офиса в казани", "not_found", "", "[]",
         "employee-secret@example.local", "Иван Тестов", "quality-test@example.local", "open"))
    svc.db.execute(
        "INSERT INTO escalations(question, norm_question, reason, division, found, user_email, user_name, "
        "to_email, status) VALUES(?,?,?,?,?,?,?,?,?)",
        ("Ещё раз: можно ли парковаться на территории офиса в Казани?",
         "можно ли парковаться на территории офиса в казани", "not_found", "", found_nf,
         "other-secret@example.local", "Пётр", "quality-test@example.local", "open"))
    svc.db.execute(
        "INSERT INTO escalations(question, norm_question, reason, division, found, user_email, user_name, "
        "to_email, status) VALUES(?,?,?,?,?,?,?,?,?)",
        ("В пресейле Дирекции ИБ меняется ли приоритет заявки, если не выдержан SLA?",
         "в пресейле дирекции иб меняется ли приоритет заявки если не выдержан sla",
         "conflict", "dib", found_conflict, "employee-secret@example.local", "Иван Тестов",
         "quality-test@example.local", "open"))
    bare = json.dumps([
        {"doc_id": holiday["doc_id"], "doc_title": holiday["title"], "clause_no": point["clause_no"],
         "ref": f"{holiday['title']}, п. {point['clause_no']}", "text": "Отпуск составляет 28 календарных дней."},
    ], ensure_ascii=False)
    svc.db.execute(
        "INSERT INTO escalations(question, norm_question, reason, division, found, user_email, user_name, "
        "to_email, status) VALUES(?,?,?,?,?,?,?,?,?)",
        ("Есть ли расхождение в сроке отпуска?", "есть ли расхождение в сроке отпуска",
         "conflict", "", bare, "employee-secret@example.local", "Иван Тестов",
         "quality-test@example.local", "open"))
    return holiday, point


def test_gaps_page_and_public_api_have_no_personal_data(services_factory):
    svc = services_factory(admin_token="s3cret-token-long-enough")
    holiday, point = _seed(svc)
    with TestClient(create_app(services=svc)) as c:
        page = c.get("/gaps")
        assert page.status_code == 200
        assert "Где не написано?" in page.text and "/static/gaps.js" in page.text
        home = c.get("/")
        assert home.status_code == 200 and 'id="gaps-link"' in home.text and 'href="/gaps"' in home.text
        r = c.get("/api/gaps")
        assert r.status_code == 200
        data = r.json()
        dumped = json.dumps(data, ensure_ascii=False)
        assert "employee-secret@example.local" not in dumped
        assert "other-secret@example.local" not in dumped
        assert "quality-test@example.local" not in dumped
        assert "Иван Тестов" not in dumped
        assert "@" not in dumped
        # unanswered grouped
        assert data["totals"]["unanswered_questions"] == 2
        assert data["totals"]["unanswered_groups"] == 1
        group = data["unanswered"][0]
        assert "парков" in group["question"].lower()
        assert group["count"] == 2
        assert len(group["ticket_ids"]) == 2
        # conflicts with links
        assert data["totals"]["conflicts"] == 2
        conf = next(x for x in data["conflicts"] if "приоритет" in x["question"].lower())
        assert len(conf["clauses"]) == 2
        assert {cl["clause_no"] for cl in conf["clauses"]} == {"5.2.2", "5.2.3"}
        for cl in conf["clauses"]:
            assert cl["link"].startswith(f"/doc/{holiday['doc_id']}?p=")
            assert cl["point_id"]
        bare_c = next(x for x in data["conflicts"] if "отпуска" in x["question"].lower())
        assert bare_c["clauses"] and "?p=" in (bare_c["clauses"][0]["link"] or "")
        assert bare_c["clauses"][0]["point_id"] == point["point_id"]
        # hot clauses
        assert data["hot_clauses"]
        assert any(h["count"] >= 1 for h in data["hot_clauses"])
        # missing refs
        mentions = " ".join(m["mention"] for m in data["missing_refs"])
        assert "17-ОД" in mentions or "приказ" in mentions.lower()
        kinds = {m["kind"] for m in data["missing_refs"]}
        assert "order" in kinds or "cancelled" in kinds or "regulation" in kinds
        assert data["corpus_conflict_scan"]["done"] is False
        assert "не выполнялся" in data["corpus_conflict_scan"]["reason"].lower()
        # export
        md = c.get("/api/gaps?format=md")
        assert md.status_code == 200
        assert "text/markdown" in md.headers.get("content-type", "")
        assert "Где не написано?" in md.text
        assert "парков" in md.text.lower()
        assert "employee-secret@" not in md.text
        csv = c.get("/api/gaps?format=csv")
        assert csv.status_code == 200
        assert "text/csv" in csv.headers.get("content-type", "")
        assert "unanswered" in csv.text and "conflict" in csv.text
        assert "employee-secret@" not in csv.text
        # still public, admin routes stay closed
        assert c.get("/api/admin/analytics").status_code in (401, 403)


def test_gaps_empty_report(services_factory):
    svc = services_factory()
    with TestClient(create_app(services=svc)) as c:
        data = c.get("/api/gaps").json()
        assert data["unanswered"] == [] and data["conflicts"] == []
        assert data["totals"]["unanswered_questions"] == 0
        assert "не выполнялся" in gaps_mod.to_markdown(data)


def test_openapi_includes_new_routes(services_factory):
    with TestClient(create_app(services=services_factory())) as c:
        schema = c.get("/openapi.json").json()
        assert "/api/simplify" in schema["paths"]
        assert "/api/gaps" in schema["paths"]
        for path in ("/api/simplify", "/api/gaps"):
            for method, op in schema["paths"][path].items():
                if method.startswith("x-"):
                    continue
                assert op.get("summary") and op.get("tags") and op.get("responses")
        assert not schema["paths"]["/api/gaps"]["get"].get("security")
        assert not schema["paths"]["/api/simplify"]["post"].get("security")
