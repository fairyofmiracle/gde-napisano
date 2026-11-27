"""Демо-ящик службы качества: письма из outbox так, как их увидел бы эксперт в почте.

Наружу ничего не отправляется: страница только читает сохранённые .eml и заявки из базы.
В продуктиве вместо этого письма уходят через SMTP/Exchange (SMTP_* в .env), ответы читаются по IMAP.
"""
from __future__ import annotations

import email
import email.policy
import json
import re
from email.utils import parsedate_to_datetime

EML_RE = re.compile(r"escalation-\d{5}-\d+\.eml")


def _eml(settings, name: str):
    if not name or not EML_RE.fullmatch(name):
        return None
    path = settings.outbox_dir / name
    if not path.is_file():
        return None
    return email.message_from_bytes(path.read_bytes(), policy=email.policy.default)


def _read_ids(db) -> dict[int, str]:
    return {int(r["esc_id"]): r["read_at"] for r in db.query("SELECT esc_id, read_at FROM mail_read")}


def _date(msg, fallback: str) -> str:
    try:
        return parsedate_to_datetime(str(msg["Date"])).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return (fallback or "")[:16]


def list_mail(db, settings, limit: int = 200) -> list[dict]:
    read = _read_ids(db)
    out = []
    for r in db.query("SELECT * FROM escalations ORDER BY id DESC LIMIT ?", (limit,)):
        msg = _eml(settings, r["eml_path"])
        subject = str(msg["Subject"]) if msg is not None and msg["Subject"] else f"[ГН-{r['id']}] {r['question'][:70]}"
        out.append({"id": r["id"], "ticket": f"ГН-{r['id']}", "subject": subject,
                    "date": _date(msg, r["created_at"]) if msg is not None else (r["created_at"] or "")[:16],
                    "from": str(msg["From"]) if msg is not None else settings.smtp_from,
                    "reason": r["reason"], "status": r["status"], "read": r["id"] in read,
                    "has_eml": msg is not None, "delivery": r["delivery"]})
    return out


def _link(db, doc_id: str, clause_no: str) -> tuple[str | None, str | None]:
    if not doc_id:
        return None, None
    rows = db.query("SELECT point_id FROM clauses WHERE doc_id=? AND clause_no=? ORDER BY ord LIMIT 1",
                    (doc_id, clause_no or ""))
    if not rows:
        return None, None
    pid = rows[0]["point_id"]
    return pid, f"/doc/{doc_id}?p={pid}"


def get_mail(db, settings, esc_id: int, mark_read: bool = True) -> dict | None:
    rows = db.query("SELECT * FROM escalations WHERE id=?", (esc_id,))
    if not rows:
        return None
    r = rows[0]
    msg = _eml(settings, r["eml_path"])
    body = ""
    if msg is not None:
        part = msg.get_body(preferencelist=("plain",))
        body = part.get_content() if part is not None else ""
    found = []
    for f in json.loads(r["found"] or "[]"):
        pid, link = _link(db, f.get("doc_id", ""), f.get("clause_no", ""))
        found.append({"ref": f.get("ref") or "", "text": f.get("text") or "", "doc_id": f.get("doc_id"),
                      "clause_no": f.get("clause_no"), "point_id": pid, "link": link})
    if mark_read:
        db.execute("INSERT OR IGNORE INTO mail_read(esc_id) VALUES(?)", (esc_id,))
    return {"id": r["id"], "ticket": f"ГН-{r['id']}",
            "subject": str(msg["Subject"]) if msg is not None else f"[ГН-{r['id']}] {r['question']}",
            "from": str(msg["From"]) if msg is not None else settings.smtp_from,
            "to": str(msg["To"]) if msg is not None else r["to_email"],
            "cc": str(msg["Cc"] or "") if msg is not None else r["user_email"],
            "date": _date(msg, r["created_at"]) if msg is not None else (r["created_at"] or "")[:16],
            "question": r["question"], "reason": r["reason"], "status": r["status"],
            "expert_answer": r["expert_answer"], "body": body, "found": found, "read": True if mark_read else None,
            "reply_url": f"/admin?esc={r['id']}#quality"}


def set_read(db, esc_id: int, read: bool) -> None:
    if read:
        db.execute("INSERT OR IGNORE INTO mail_read(esc_id) VALUES(?)", (esc_id,))
    else:
        db.execute("DELETE FROM mail_read WHERE esc_id=?", (esc_id,))
