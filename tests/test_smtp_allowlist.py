"""Настоящая почта только на разрешённые адреса; остальное остаётся в outbox."""
from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace

import app.escalation as esc_mod
from app.escalation import EscalationService

BOT = "s5s345kg@mail.grokbot.com"


class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg, to_addrs=None):
        FakeSMTP.sent.append(to_addrs)


def _svc(tmp_path, allow):
    s = SimpleNamespace(smtp_host="smtp.test.invalid", smtp_port=2525, smtp_timeout=1, smtp_ssl=False,
                        smtp_starttls=True, smtp_user="", smtp_password="", smtp_allowed_recipients=allow,
                        outbox_dir=tmp_path / "outbox")
    return EscalationService(s, None, None)


def _msg(to, cc=""):
    m = EmailMessage()
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Subject"] = "t"
    m.set_content("x")
    return m


def test_only_allowed_recipient_gets_real_mail(tmp_path, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(esc_mod.smtplib, "SMTP", FakeSMTP)
    svc = _svc(tmp_path, BOT)
    delivery, path = svc._deliver(1, _msg(BOT, "employee-test@example.local"))
    assert delivery == "smtp" and FakeSMTP.sent == [[BOT]]
    assert (tmp_path / "outbox" / path).exists()  # копия для операторской остаётся


def test_not_allowed_recipients_stay_in_outbox(tmp_path, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(esc_mod.smtplib, "SMTP", FakeSMTP)
    svc = _svc(tmp_path, BOT)
    delivery, _ = svc._deliver(2, _msg("quality-test@example.local", "employee-test@example.local"))
    assert delivery == "outbox" and FakeSMTP.sent == []


def test_case_insensitive_and_no_allowlist_means_no_filter(tmp_path):
    svc = _svc(tmp_path, " S5S345KG@mail.grokbot.com , other@x.local")
    assert svc.allowed_recipients(_msg(f"Бот <{BOT}>")) == [BOT]
    assert _svc(tmp_path, "").allowed_recipients(_msg(BOT)) is None


def test_smtp_failure_falls_back_to_outbox(tmp_path, monkeypatch):
    class Down(FakeSMTP):
        def __init__(self, *a, **k):
            raise OSError("closed")

    monkeypatch.setattr(esc_mod.smtplib, "SMTP", Down)
    delivery, _ = _svc(tmp_path, BOT)._deliver(3, _msg(BOT))
    assert delivery == "outbox"
