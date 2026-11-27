"""Выгрузка хранилища в Confluence: архив, якоря, связи пунктов и разъяснений, загрузка по REST API."""
from __future__ import annotations

import io
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import confluence
from app.main import create_app
from tests.test_answers import _load

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import confluence_upload  # noqa: E402

NS = ('<root xmlns:ac="http://atlassian.com/content" xmlns:ri="http://atlassian.com/resource/identifier">'
      '{}</root>')


def _zip(svc) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(confluence.build_zip(svc.db, svc.boxes)))


def _add_faq(svc, doc_id, title, clause_no):
    svc.db.execute("INSERT INTO faq(question, answer, doc_id, doc_title, clause_no, escalation_id) VALUES(?,?,?,?,?,?)",
                   ("Можно ли делить отпуск <на> части?", "ОТВЕТ: да, по согласованию & с кадрами.", doc_id, title,
                    clause_no, 5))


def test_archive_has_pages_anchors_and_links(services_factory):
    svc = services_factory()
    _load(svc)
    doc = svc.db.query("SELECT doc_id, title FROM files ORDER BY title LIMIT 1")[0]
    cl = svc.db.query("SELECT clause_no FROM clauses WHERE doc_id=? AND clause_no!='' ORDER BY ord LIMIT 1",
                      (doc["doc_id"],))[0]["clause_no"]
    _add_faq(svc, doc["doc_id"], doc["title"], cl)
    z = _zip(svc)
    man = json.loads(z.read("manifest.json"))
    titles = [p["title"] for p in man["pages"]]
    assert titles[0] == confluence.ROOT_TITLE and titles[-1] == confluence.FAQ_TITLE
    assert doc["title"] in titles
    assert all(p["parent"] == confluence.ROOT_TITLE for p in man["pages"][1:])
    for p in man["pages"]:
        ET.fromstring(NS.format(z.read(p["storage"]).decode()))  # корректный XML с пространствами имён
    page = next(p for p in man["pages"] if p["title"] == doc["title"])
    st = z.read(page["storage"]).decode()
    anchor = confluence.anchor_name(cl, 0)
    assert f'<ac:parameter ac:name="">{anchor}</ac:parameter>' in st
    assert 'ac:anchor="faq-1"' in st  # пункт → разъяснение
    faq = z.read("storage/99-faq.xml").decode()
    assert f'ac:anchor="{anchor}"' in faq and f'ri:content-title="{doc["title"]}"' in faq  # разъяснение → пункт
    assert "ГН-5" in faq and "&lt;на&gt;" in faq and "&amp;" in faq
    h = z.read(page["html"]).decode()
    assert f'id="{anchor}"' in h and 'href="faq.html#faq-1"' in h
    hf = z.read("html/faq.html").decode()
    assert re.search(rf'href="{page["html"][5:]}#{re.escape(anchor)}"', hf)


def test_parts_of_one_clause_are_merged_and_anchors_unique(services_factory):
    svc = services_factory()
    _load(svc)
    data = confluence.collect(svc.db, svc.boxes)
    for d in data["documents"]:
        names = [c["anchor"] for c in d["clauses"]]
        assert len(names) == len(set(names))
    n_points = svc.db.query("SELECT COUNT(*) AS n FROM clauses")[0]["n"]
    assert sum(len(c["point_ids"]) for d in data["documents"] for c in d["clauses"]) == n_points


def test_export_endpoint_and_wiki_button(services_factory):
    with TestClient(create_app(services=services_factory())) as c:
        _load(c.app.state.services)
        r = c.get("/api/wiki/export")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert "gde-napisano-confluence.zip" in r.headers["content-disposition"]
        assert "manifest.json" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert 'href="/api/wiki/export"' in c.get("/wiki").text


def test_upload_creates_hierarchy_and_updates_existing(services_factory, tmp_path):
    svc = services_factory()
    _load(svc)
    path = tmp_path / "x.zip"
    path.write_bytes(confluence.build_zip(svc.db, svc.boxes))
    pages, bodies = confluence_upload.load(str(path))
    calls = []
    existing = {pages[1]["title"]: {"id": "77", "version": {"number": 3}}}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.method == "GET":
            t = req.url.params["title"]
            return httpx.Response(200, json={"results": [existing[t]] if t in existing else []})
        body = json.loads(req.content)
        if req.method == "PUT":
            assert body["version"]["number"] == 4
            return httpx.Response(200, json={"id": "77"})
        return httpx.Response(200, json={"id": str(100 + len(calls))})

    with httpx.Client(base_url="https://conf.test.invalid/wiki", transport=httpx.MockTransport(handler)) as cl:
        ids = confluence_upload.upload(pages, bodies, cl, "GN")
    posts = [json.loads(r.content) for r in calls if r.method in ("POST", "PUT")]
    assert "ancestors" not in posts[0]
    assert all(p["ancestors"] == [{"id": ids[confluence.ROOT_TITLE]}] for p in posts[1:])
    assert all(p["body"]["storage"]["representation"] == "storage" and p["space"]["key"] == "GN" for p in posts)
    assert ids[pages[1]["title"]] == "77"
