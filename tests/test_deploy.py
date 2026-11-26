"""Проверки конфигурации развёртывания: compose, Caddy, Dockerfile, игнор-файлы, скрипты."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CADDY = shutil.which("caddy") or os.environ.get("CADDY_BIN", "")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_only_proxy_is_public(compose):
    for name, svc in compose["services"].items():
        if name == "caddy":
            continue
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name}: {port} открыт наружу"
    ports = compose["services"]["caddy"]["ports"]
    assert "${PUBLIC_BIND:-127.0.0.1}:${HTTP_PORT:-80}:80" in ports  # наружу только по явному PUBLIC_BIND=0.0.0.0
    assert "${PUBLIC_BIND:-127.0.0.1}:${HTTPS_PORT:-443}:443" in ports


def test_proxy_ip_matches_forwarded_allow_ips(compose):
    s = compose["services"]
    caddy_ip = s["caddy"]["networks"]["backend"]["ipv4_address"]
    bot_ip = s["bot"]["networks"]["backend"]["ipv4_address"]
    assert s["app"]["environment"]["FORWARDED_ALLOW_IPS"] == caddy_ip
    assert s["app"]["environment"]["TRUSTED_CLIENT_IPS"] == bot_ip
    rng = compose["networks"]["backend"]["ipam"]["config"][0]["ip_range"]
    assert rng.endswith(".128/25") and int(caddy_ip.rsplit(".", 1)[1]) < 128 and int(bot_ip.rsplit(".", 1)[1]) < 128


def test_restart_healthchecks_and_certs(compose):
    s = compose["services"]
    assert all(svc.get("restart") == "unless-stopped" for svc in s.values())
    assert "healthcheck" in s["qdrant"] and "healthcheck" in s["app"]
    assert s["app"]["depends_on"]["qdrant"]["condition"] == "service_healthy"
    assert "./certs:/certs:ro" in s["app"]["volumes"]
    assert "/ready" in " ".join(s["app"]["healthcheck"]["test"])


def test_dockerfile_proxy_headers_and_prefetch():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '"--proxy-headers"' in text and "FORWARDED_ALLOW_IPS=127.0.0.1" in text
    assert "ARG PREFETCH_MODELS" in text and "prefetch_models.py" in text
    assert "/ready" in text


def test_caddyfile_contents():
    text = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "reverse_proxy {$UPSTREAM:app:8000}" in text
    assert "request_body" in text and "max_size" in text
    assert "{$SITE_ADDRESS::80}" in text and "timeouts" in text
    # заголовки запроса (X-Admin-Token, куки) в журнал не пишутся
    assert "request>headers delete" in text


@pytest.mark.skipif(not CADDY, reason="нет caddy (можно указать CADDY_BIN)")
@pytest.mark.parametrize("site", [":80", "docs.example.ru"])
def test_caddyfile_validates(site):
    env = dict(os.environ, SITE_ADDRESS=site)
    r = subprocess.run([CADDY, "adapt", "--config", str(ROOT / "deploy" / "Caddyfile"), "--adapter", "caddyfile"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr


def test_ignore_files():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for item in (".env", "static_backup_*/", ".pytest_cache/", "data/runtime/", "backups/", "certs/*"):
        assert item in gi
    assert "!.env.example" in gi
    di = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for item in (".env", "static_backup_*", "tests", "backups", ".git"):
        assert item in di


@pytest.mark.skipif(shutil.which("bash") is None, reason="нет bash")
@pytest.mark.parametrize("script", ["backup.sh", "restore.sh", "prefetch_models.sh", "get_russian_ca.sh"])
def test_shell_scripts_parse(script):
    path = ROOT / "scripts" / script
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="нет bash")
def test_native_backup_and_restore_roundtrip(tmp_path):
    """Копия и восстановление без Docker: база, документы, повторная копия вытесняет старые."""
    import sqlite3

    data = tmp_path / "data"
    (data / "docs" / "hr").mkdir(parents=True)
    (data / "docs" / "hr" / "a.txt").write_text("1.1. Отпуск 28 дней.", encoding="utf-8")
    (data / "qdrant").mkdir()
    (data / "qdrant" / "big.bin").write_bytes(b"x" * 100)
    db = sqlite3.connect(data / "docagent.sqlite3")
    db.execute("CREATE TABLE t(v)")
    db.execute("INSERT INTO t VALUES ('да')")
    db.commit()
    db.close()
    out = tmp_path / "backups"
    env = dict(os.environ, NATIVE_DATA_DIR=str(data), BACKUP_DIR=str(out))
    subprocess.run(["bash", str(ROOT / "scripts" / "backup.sh")], check=True, env=env, capture_output=True)
    archives = list(out.glob("gde-napisano-*.tar.gz"))
    assert len(archives) == 1
    # потеря данных
    shutil.rmtree(data / "docs")
    (data / "docagent.sqlite3").unlink()
    subprocess.run(["bash", str(ROOT / "scripts" / "restore.sh"), str(archives[0])], check=True, env=env, capture_output=True)
    assert (data / "docs" / "hr" / "a.txt").read_text(encoding="utf-8") == "1.1. Отпуск 28 дней."
    assert sqlite3.connect(data / "docagent.sqlite3").execute("SELECT v FROM t").fetchone() == ("да",)
    assert not (data / "backup.sqlite3").exists()
    assert (data / "qdrant" / "big.bin").exists()  # индекс не трогаем: он пересобирается или хранится отдельно
