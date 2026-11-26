from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.security import safe_filename, safe_join


@pytest.mark.parametrize("name,expected", [
    ("../../evil.txt", "evil.txt"),
    ("..\\..\\Windows\\evil.docx", "evil.docx"),
    ("C:\\Users\\x\\Положение об отпусках.docx", "Положение об отпусках.docx"),
    ("/etc/passwd.txt", "passwd.txt"),
    ("normal<>|:*?.pdf", "normal_.pdf"),
    ("CON.txt", "_CON.txt"),
    ("Отчёт.PDF", "Отчёт.pdf"),
])
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


@pytest.mark.parametrize("name", ["..", "../", "evil.exe", "noext", ".txt", ""])
def test_safe_filename_rejects(name):
    with pytest.raises(ValueError):
        safe_filename(name)


def test_safe_join_blocks_escape(tmp_path):
    assert safe_join(tmp_path, "hr", "a.txt").is_relative_to(tmp_path.resolve())
    with pytest.raises(ValueError):
        safe_join(tmp_path, "..", "a.txt")


ADMIN_CALLS = [
    ("get", "/api/admin/documents"), ("get", "/api/admin/stats"), ("post", "/api/admin/reindex"),
    ("get", "/api/operator/inbox"), ("delete", "/api/admin/documents/abc"),
]


def test_admin_closed_without_token(services_factory):
    svc = services_factory(admin_token="")
    with TestClient(create_app(services=svc)) as c:
        for method, url in ADMIN_CALLS:
            assert getattr(c, method)(url, headers={"X-Admin-Token": ""}).status_code == 403
        r = c.post("/api/admin/upload", files={"file": ("a.txt", b"1.1. x")})
        assert r.status_code == 403
        assert c.get("/api/health").json()["admin_enabled"] is False


def test_admin_requires_right_token(services_factory):
    svc = services_factory(admin_token="s3cret-token-long-enough")
    with TestClient(create_app(services=svc)) as c:
        for method, url in ADMIN_CALLS[:4]:
            assert getattr(c, method)(url).status_code == 401
            assert getattr(c, method)(url, headers={"X-Admin-Token": "wrong"}).status_code == 401
        assert c.get("/api/admin/documents", headers={"X-Admin-Token": "s3cret-token-long-enough"}).status_code == 200
        assert c.get("/api/admin/stats", headers={"Authorization": "Bearer s3cret-token-long-enough"}).status_code == 200


def test_upload_traversal_stays_inside_docs(services_factory, tmp_path):
    svc = services_factory(admin_token="t0ken-123-long-enough")
    h = {"X-Admin-Token": "t0ken-123-long-enough"}
    with TestClient(create_app(services=svc)) as c:
        r = c.post("/api/admin/upload", headers=h, data={"box": "hr"},
                   files={"file": ("../../evil.txt", "1.1. Отпуск 28 дней.".encode())})
        assert r.status_code == 200, r.text
        assert r.json()["file"] == "hr/evil.txt"
        assert (svc.indexer.docs_dir / "hr" / "evil.txt").exists()
        assert not (tmp_path / "evil.txt").exists() and not (svc.indexer.docs_dir.parent / "evil.txt").exists()
        bad_box = c.post("/api/admin/upload", headers=h, data={"box": "../.."},
                         files={"file": ("a.txt", b"1.1. x")})
        assert bad_box.status_code == 400
        bad_ext = c.post("/api/admin/upload", headers=h, files={"file": ("run.sh", b"echo")})
        assert bad_ext.status_code == 400


def test_no_keyword_escalation(services_factory):
    """Слова вроде «оператор» или «жалоба» не должны сами по себе что-то запускать."""
    svc = services_factory()
    svc.indexer.docs_dir.mkdir(parents=True, exist_ok=True)
    (svc.indexer.docs_dir / "a.txt").write_text("1.1. Отпуск составляет 28 календарных дней.", encoding="utf-8")
    svc.indexer.sync()
    ans = svc.answers.ask("позовите оператора, жалоба на отпуск 28 календарных дней")
    assert ans["mode"] in ("confident", "uncertain", "not_found")
    assert "escalated" not in ans


def test_demo_ui_only_forces_fake_models(tmp_path):
    from tests.conftest import make_settings

    s = make_settings(tmp_path, demo_ui_only=True, embed_provider="e5", reranker_provider="cross-encoder",
                      llm_provider="ollama")
    assert (s.embed_provider, s.reranker_provider, s.llm_provider) == ("fake", "fake", "fake")
