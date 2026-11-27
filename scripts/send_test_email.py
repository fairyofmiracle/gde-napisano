"""Пробное письмо через SMTP из .env, чтобы проверить настройку почты.

    python scripts/send_test_email.py quality@company.local

Адрес должен входить в SMTP_ALLOWED_RECIPIENTS: на другие адреса скрипт не отправляет.
Пароль и другие секреты не печатаются.
"""
from __future__ import annotations

import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.escalation import EscalationService  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    to = sys.argv[1].strip()
    s = Settings()
    allow = [a.strip().lower() for a in (s.smtp_allowed_recipients or "").split(",") if a.strip()]
    if not s.smtp_host:
        print("SMTP_HOST не задан: отправка выключена, письма остаются в outbox")
        return 1
    if to.lower() not in allow:
        print("Адрес не входит в SMTP_ALLOWED_RECIPIENTS, не отправляю")
        return 1
    msg = EmailMessage()
    msg["Subject"] = "[ГН-тест] Проверка почты стенда «Где написано?»"
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="gde-napisano.local")
    msg.set_content("Пробное письмо со стенда «Где написано?». Отвечать на него не нужно.")
    EscalationService(s, None, None).send(msg)
    print(f"отправлено через {s.smtp_host}:{s.smtp_port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
