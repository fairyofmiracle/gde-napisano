"""Подпункты нумерованных списков: метка для отображения, point_id не меняется."""
from __future__ import annotations

from app.parsing.structure import sub_item_labels

DOC = (
    "1. Постановка задачи\n"
    "1.1. Подготовка начинается с запроса заказчика на расчёт поставки.\n"
    "1.3. Если заказчик выбрал продукт и запрашивает только поставку, то:\n"
    "1. Менеджер создаёт заявку на расчёт в учётной системе.\n"
    "2. Если подходящего предложения нет, запрос передаётся архитектору.\n"
    "2. Проработка запроса\n"
    "2.1. Архитектор выполняет первичную квалификацию запроса.\n"
)


def test_labels_pure():
    rows = [("a", "clause", "S1", "1.3"), ("b", "clause", "S1", "1"), ("c", "clause", "S1", "2"),
            ("d", "clause", "S2", "1"), ("e", "clause", "S2", "2.1"), ("f", "table_row", "S2", "3")]
    assert sub_item_labels(rows) == {"b": "1.3, подп. 1", "c": "1.3, подп. 2"}


def test_labels_do_not_touch_point_ids(services_factory):
    svc = services_factory()
    docs = svc.indexer.docs_dir
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "presale.txt").write_text(DOC, encoding="utf-8")
    svc.indexer.sync()
    rows = svc.db.query("SELECT point_id, clause_no, label, text_hash FROM clauses ORDER BY ord")
    before = [(r["point_id"], r["clause_no"], r["text_hash"]) for r in rows]
    labels = {r["clause_no"] + ":" + r["point_id"][:4]: r["label"] for r in rows}
    subs = [r for r in rows if r["label"]]
    assert [r["label"] for r in subs] == ["1.3, подп. 1", "1.3, подп. 2"], labels
    assert [r["clause_no"] for r in subs] == ["1", "2"]  # номер в базе прежний
    # повторный пересчёт ничего не меняет, point_id те же
    assert svc.indexer.refresh_labels() == 0
    svc.indexer.sync()
    after = [(r["point_id"], r["clause_no"], r["text_hash"])
             for r in svc.db.query("SELECT point_id, clause_no, text_hash FROM clauses ORDER BY ord")]
    assert after == before

    cand = svc.searcher._load([subs[0]["point_id"]])[0]
    if cand is not None:
        assert cand.ref.endswith("п. 1.3, подп. 1")
        assert cand.clause_no == "1"


def test_clause_api_shows_label(services_factory):
    from fastapi.testclient import TestClient

    from app.main import create_app

    svc = services_factory()
    docs = svc.indexer.docs_dir
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "presale.txt").write_text(DOC, encoding="utf-8")
    svc.indexer.sync()
    sub = svc.db.query("SELECT point_id, doc_id FROM clauses WHERE label<>'' ORDER BY ord")[0]
    with TestClient(create_app(services=svc)) as client:
        r = client.get(f"/api/clauses/{sub['point_id']}").json()
        d = client.get(f"/api/documents/{sub['doc_id']}/clauses").json()
    assert r["ref"].endswith("п. 1.3, подп. 1") and r["clause_no"] == "1" and r["label"] == "1.3, подп. 1"
    assert any(c.get("label") == "1.3, подп. 1" for c in d["clauses"])
