from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from tests.conftest import make_settings


def _client(services_factory, **kw):
    return TestClient(create_app(services=services_factory(**kw)))


def test_liveness_readiness_version(services_factory):
    with _client(services_factory) as c:
        h = c.get("/health")
        assert h.status_code == 200
        assert h.json()["status"] == "ok"
        assert "X-Request-ID" in h.headers
        r = c.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["demo_ui_only"] is False
        assert body["checks"]["qdrant"] is True
        assert body["checks"]["llm"] is True
        assert body["checks"]["redis"] is None
        v = c.get("/version")
        assert v.status_code == 200
        data = v.json()
        assert data["version"]
        assert data["llm_provider"] == "fake"
        assert data["embed_provider"] == "fake"
        assert data["demo_ui_only"] is False
        dumped = str(data).lower()
        assert "admin_token" not in dumped and "credentials" not in dumped
        assert "token" not in dumped


def test_ready_and_version_mark_demo(services_factory):
    with _client(services_factory, demo_ui_only=True) as c:
        r = c.get("/ready").json()
        assert r["demo_ui_only"] is True and r["status"] == "ok"
        v = c.get("/version").json()
        assert v["demo_ui_only"] is True
        assert v["llm_provider"] == v["embed_provider"] == v["reranker_provider"] == "fake"


def test_ready_503_when_qdrant_down(services_factory):
    svc = services_factory()
    with TestClient(create_app(services=svc)) as c:
        svc.store.healthy = lambda: False
        r = c.get("/ready")
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready"
        assert r.json()["checks"]["qdrant"] is False
        assert r.headers.get("X-Request-ID")


def test_docs_redoc_openapi(services_factory):
    with _client(services_factory) as c:
        assert c.get("/docs").status_code == 200
        assert c.get("/redoc").status_code == 200
        schema = c.get("/openapi.json").json()
        assert schema["info"]["title"]
        assert schema["info"]["version"]
        assert schema["info"]["description"]
        tag_names = {t["name"] for t in schema["tags"]}
        assert tag_names == {"Служебное", "Чат и поиск", "Документы", "Ящички", "Отзывы", "Админ"}
        schemes = schema["components"]["securitySchemes"]
        assert schemes["BearerAuth"]["scheme"] == "bearer"
        assert schemes["AdminToken"]["name"] == "X-Admin-Token"
        admin = schema["paths"]["/api/admin/documents"]["get"]
        assert admin["security"] == [{"BearerAuth": []}, {"AdminToken": []}]
        assert "Админ" in admin["tags"] or "Документы" in admin["tags"]
        public = schema["paths"]["/api/chat"]["post"]
        assert not public.get("security")
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                if method.startswith("x-") or not isinstance(op, dict):
                    continue
                assert op.get("summary"), f"{method} {path} без summary"
                assert op.get("tags"), f"{method} {path} без tags"
                assert op.get("responses"), f"{method} {path} без responses"


def test_request_id_echo_and_generation(services_factory):
    with _client(services_factory) as c:
        r = c.get("/health", headers={"X-Request-ID": "client.req-1"})
        assert r.headers["X-Request-ID"] == "client.req-1"
        r2 = c.get("/health")
        assert r2.headers["X-Request-ID"]
        assert r2.headers["X-Request-ID"] != "client.req-1"


def test_validation_error_format(services_factory):
    with _client(services_factory) as c:
        r = c.post("/api/chat", json={"question": "x"})
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == "validation_error"
        assert body["error"]["message"] == "Некорректные данные запроса"
        assert body["detail"] == body["error"]["message"]
        assert body["request_id"] == r.headers["X-Request-ID"]
        assert any("question" in str(item.get("loc")) for item in body["error"]["details"])


def test_not_found_and_bad_feedback_format(services_factory):
    with _client(services_factory) as c:
        r = c.get("/api/clauses/no-such-point")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"
        assert r.json()["detail"]
        missing = c.post("/api/feedback", json={"answer_id": "missing-answer-id", "helpful": True})
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"


def test_chat_rate_limit(services_factory):
    with _client(services_factory, rate_limit_per_minute=2) as c:
        payload = {"question": "какой отпуск положен сотруднику?"}
        assert c.post("/api/chat", json=payload).status_code == 200
        assert c.post("/api/chat", json=payload).status_code == 200
        r = c.post("/api/chat", json=payload)
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "rate_limited"
        assert r.json()["detail"]
        assert r.headers.get("Retry-After") == "60"


def test_cors_from_settings(services_factory):
    with _client(services_factory, cors_origins="http://example.com") as c:
        ok = c.get("/health", headers={"Origin": "http://example.com"})
        assert ok.headers.get("access-control-allow-origin") == "http://example.com"
        denied = c.get("/health", headers={"Origin": "http://evil.example"})
        assert denied.headers.get("access-control-allow-origin") != "http://evil.example"


def test_upload_limits_from_settings(services_factory):
    h = {"X-Admin-Token": "t0ken-123-long-enough"}
    with _client(services_factory, admin_token="t0ken-123-long-enough", allowed_upload_extensions=".txt",
                 max_upload_mb=0) as c:
        denied = c.post("/api/admin/upload", headers=h, files={"file": ("note.md", "1.1. текст".encode())})
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "bad_request"
        huge = c.post("/api/admin/upload", headers=h, files={"file": ("note.txt", "1.1. слишком большой".encode())})
        assert huge.status_code == 413
        assert huge.json()["error"]["code"] == "payload_too_large"


def test_unhandled_error_hides_internals(services_factory):
    svc = services_factory()
    with TestClient(create_app(services=svc), raise_server_exceptions=False) as c:
        svc.boxes.ids = lambda: (_ for _ in ()).throw(RuntimeError("секрет не должен утечь"))
        r = c.get("/api/boxes")
        assert r.status_code == 500
        body = r.json()
        assert body["error"]["code"] == "internal_error"
        assert body["detail"] == "Внутренняя ошибка сервера"
        assert "секрет" not in r.text
        assert r.headers.get("X-Request-ID")


def test_legacy_health_still_works(services_factory):
    with _client(services_factory, admin_token="") as c:
        r = c.get("/api/health")
        assert r.status_code == 200
        assert r.json()["admin_enabled"] is False
        assert r.json()["status"] == "ok"


def test_invalid_allowed_extensions_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, allowed_upload_extensions=".exe,.txt")
