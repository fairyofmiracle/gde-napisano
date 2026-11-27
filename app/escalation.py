"""Эскалация в службу качества письмом и разбор ответа эксперта.

Письмо уходит на QUALITY_EMAIL, сотрудник в копии. Если SMTP не настроен, письмо сохраняется
в DATA_DIR/outbox как .eml (его видно в операторской). Ответ эксперта приходит:
- по IMAP (IMAP_HOST и т. д.),
- импортом .eml в операторской,
- через форму «ответ эксперта» в операторской (имитация почты).
Финальный ответ попадает в базу разъяснений (FAQ). Сами регламенты не меняются.
"""
from __future__ import annotations

import email
import email.policy
import json
import logging
import re
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from app.text import normalize

log = logging.getLogger(__name__)

REASONS = {
    "not_found": "в регламентах нет ответа на вопрос",
    "low_confidence": "найденные пункты не дают однозначного ответа (недостаточная уверенность)",
    "conflict": "найденные пункты противоречат друг другу",
    "manual": "сотрудник попросил передать вопрос в службу качества",
}
SUBJECT_TAG = re.compile(r"\[ГН-(\d+)\]")
_FINAL_TAG = re.compile(r"^\s*(?:финальный\s+)?ответ\s*[.:]\s*", re.I)
_NOT_FINAL = re.compile(r"^\s*(?:вопрос|уточн\w+|нужн\w+\s+(?:уточнить|информаци\w*))\b", re.I)
_QUOTE_START = re.compile(
    r"^(?:>|-{2,}\s*(?:original|исходное|пересылаемое)|on .+ wrote:|.+\s+пишет:|\d{1,2}[./]\d{1,2}[./]\d{2,4}.+:$"
    r"|from:|от:|отправлено:)", re.I)


class EscalationError(Exception):
    pass


class EscalationService:
    def __init__(self, settings, db, faq) -> None:
        self.settings = settings
        self.db = db
        self.faq = faq

    # --- создание ----------------------------------------------------------------------
    def create(self, question: str, reason: str, found: list[dict] | None = None, *, user_email: str = "",
               user_name: str = "", division: str = "", division_name: str = "", answer_id: str = "",
               session_id: str = "") -> dict:
        s = self.settings
        norm = normalize(question)
        user_email = (user_email or s.demo_user_email).strip()
        user_name = (user_name or s.demo_user_name).strip() or "Сотрудник"
        if not s.quality_email:
            raise EscalationError("QUALITY_EMAIL не задан: некуда отправить запрос")
        # Тот же вопрос в той же сессии уже передан и ещё без ответа — второе письмо не шлём
        if session_id:
            same = self.db.query("SELECT * FROM escalations WHERE norm_question=? AND session_id=? AND status='open' "
                                 "AND created_at >= datetime('now', '-1 day') ORDER BY id DESC LIMIT 1",
                                 (norm, session_id))
            if same:
                return self._public(same[0], duplicate=True)
        found = (found or [])[:5]
        cur = self.db.execute(
            "INSERT INTO escalations(answer_id, session_id, question, norm_question, reason, division, found,"
            " user_email, user_name, to_email) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (answer_id, session_id, question, norm, reason, division, json.dumps(found, ensure_ascii=False),
             user_email, user_name, s.quality_email))
        esc_id = int(cur.lastrowid)
        msg = self.build_message(esc_id, question, reason, found, user_email, user_name, division_name)
        delivery, path = self._deliver(esc_id, msg)
        self.db.execute("UPDATE escalations SET message_id=?, delivery=?, eml_path=? WHERE id=?",
                        (msg["Message-ID"], delivery, path, esc_id))
        self.db.execute("INSERT INTO events(kind, answer_id, session_id, detail) VALUES('escalation', ?, ?, ?)",
                        (answer_id, session_id, json.dumps({"id": esc_id, "reason": reason}, ensure_ascii=False)))
        return self._public(self.get(esc_id))

    def build_message(self, esc_id: int, question: str, reason: str, found: list[dict], user_email: str,
                      user_name: str, division_name: str = "") -> EmailMessage:
        s = self.settings
        msg = EmailMessage()
        short = " ".join(question.split())
        short = short if len(short) <= 70 else short[:69].rstrip() + "…"
        msg["Subject"] = f"[ГН-{esc_id}] Вопрос без ответа в регламентах: {short}"
        msg["From"] = s.smtp_from
        msg["To"] = s.quality_email
        if user_email:
            msg["Cc"] = user_email
        msg["Reply-To"] = s.smtp_from
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(idstring=f"gn{esc_id}.{secrets.token_hex(4)}", domain="gde-napisano.local")
        msg["X-GN-Escalation"] = str(esc_id)
        lines = [
            "Здравствуйте!",
            "",
            "Сотрудник задал вопрос, на который бот «Где написано?» не смог дать подтверждённый ответ по регламентам.",
            "",
            f"Вопрос: {question}",
            f"Сотрудник: {user_name} <{user_email}>" if user_email else f"Сотрудник: {user_name}",
        ]
        if division_name:
            lines.append(f"Подразделение: {division_name}")
        lines.append(f"Причина: {REASONS.get(reason, reason)}")
        if found:
            lines += ["", "Что нашлось в регламентах (не подтверждено как ответ):"]
            for f in found:
                snippet = " ".join(str(f.get("text") or "").split())
                snippet = snippet if len(snippet) <= 300 else snippet[:299] + "…"
                lines.append(f"- {f.get('ref', '')}: {snippet}")
        else:
            lines += ["", "Подходящих пунктов в регламентах не нашлось."]
        lines += [
            "",
            "Пожалуйста, ответьте на это письмо. Ответ, который начинается со слова «ОТВЕТ:» или не содержит "
            "встречного вопроса, считается финальным: бот добавит его в базу разъяснений службы качества "
            "и будет отвечать им на такие же вопросы. Сами регламенты бот не меняет.",
            "Если нужно уточнение, начните письмо со слова «ВОПРОС:».",
        ]
        if s.web_public_url:
            lines += ["", f"Операторская: {s.web_public_url.rstrip('/')}/admin"]
        lines += ["", "--", "Где написано? Ответы по корпоративным регламентам"]
        msg.set_content("\n".join(lines))
        return msg

    def _deliver(self, esc_id: int, msg: EmailMessage) -> tuple[str, str]:
        s = self.settings
        if s.smtp_host:
            try:
                ctx = ssl.create_default_context()
                if s.smtp_ssl:
                    server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout, context=ctx)
                else:
                    server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout)
                with server:
                    if s.smtp_starttls and not s.smtp_ssl:
                        server.starttls(context=ctx)
                    if s.smtp_user:
                        server.login(s.smtp_user, s.smtp_password)
                    server.send_message(msg)
                return "smtp", self._save(esc_id, msg)
            except (OSError, smtplib.SMTPException) as e:
                log.error("Письмо в службу качества не отправлено (%s), сохраняю в outbox", type(e).__name__)
        return "outbox", self._save(esc_id, msg)

    def _save(self, esc_id: int, msg: EmailMessage) -> str:
        out = self.settings.outbox_dir
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"escalation-{esc_id:05d}-{int(time.time())}.eml"
        path.write_bytes(msg.as_bytes(policy=email.policy.SMTP))
        return path.name

    # --- чтение ------------------------------------------------------------------------
    def get(self, esc_id: int):
        rows = self.db.query("SELECT * FROM escalations WHERE id=?", (esc_id,))
        return rows[0] if rows else None

    def list(self, limit: int = 100) -> list[dict]:
        return [self._public(r) for r in self.db.query("SELECT * FROM escalations ORDER BY id DESC LIMIT ?", (limit,))]

    def _public(self, r, duplicate: bool = False) -> dict:
        return {"id": r["id"], "created_at": r["created_at"], "question": r["question"], "reason": r["reason"],
                "reason_text": REASONS.get(r["reason"], r["reason"]), "division": r["division"],
                "to": r["to_email"], "cc": r["user_email"], "delivery": r["delivery"], "eml": r["eml_path"],
                "status": r["status"], "expert_answer": r["expert_answer"], "answered_at": r["answered_at"],
                "faq_id": r["faq_id"], "found": json.loads(r["found"] or "[]"), "duplicate": duplicate}

    # --- ответ эксперта ----------------------------------------------------------------
    def answer(self, esc_id: int, text: str, expert: str = "", final: bool | None = None) -> dict:
        r = self.get(esc_id)
        if r is None:
            raise KeyError(esc_id)
        body = extract_reply(text)
        if not body:
            raise EscalationError("В ответе эксперта нет текста")
        is_final = is_final_answer(body) if final is None else final
        body = capitalize_first(_FINAL_TAG.sub("", body).strip())
        if not is_final:
            self.db.execute("UPDATE escalations SET status='clarification', expert_answer=?, expert_from=? WHERE id=?",
                            (body, expert, esc_id))
            return {"id": esc_id, "final": False, "faq_id": None}
        found = json.loads(r["found"] or "[]")
        top = found[0] if found else {}
        faq_id = self.faq.add(r["question"], body, doc_id=top.get("doc_id", ""), doc_title=top.get("doc_title", ""),
                              clause_no=top.get("clause_no", ""), division=r["division"], expert=expert,
                              escalation_id=esc_id)
        self.db.execute("UPDATE escalations SET status='answered', expert_answer=?, expert_from=?, "
                        "answered_at=datetime('now'), faq_id=? WHERE id=?", (body, expert, faq_id, esc_id))
        self.db.execute("INSERT INTO events(kind, detail) VALUES('expert_answer', ?)",
                        (json.dumps({"id": esc_id, "faq_id": faq_id}),))
        return {"id": esc_id, "final": True, "faq_id": faq_id}

    def import_eml(self, raw: bytes) -> dict:
        """Письмо-ответ эксперта (.eml). Заявку ищем по [ГН-n] в теме или по In-Reply-To."""
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        esc_id = self._match(msg)
        if esc_id is None:
            raise EscalationError("Не удалось понять, на какой запрос этот ответ: нет [ГН-номер] в теме")
        part = msg.get_body(preferencelist=("plain", "html"))
        text = part.get_content() if part is not None else ""
        if part is not None and part.get_content_type() == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        sender = parseaddr(msg.get("From", ""))[1]
        return self.answer(esc_id, text, expert=sender)

    def _match(self, msg) -> int | None:
        m = SUBJECT_TAG.search(str(msg.get("Subject", "")))
        if m:
            return int(m.group(1))
        refs = f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}"
        for mid in re.findall(r"<[^>]+>", refs):
            rows = self.db.query("SELECT id FROM escalations WHERE message_id=?", (mid,))
            if rows:
                return int(rows[0]["id"])
        return None

    def poll_imap(self, imap_factory=None) -> dict:
        """Читает непрочитанные ответы с [ГН-n] в теме и разбирает их."""
        s = self.settings
        if not s.imap_host:
            raise EscalationError("IMAP не настроен (IMAP_HOST пуст)")
        import imaplib

        factory = imap_factory or (lambda: imaplib.IMAP4_SSL(s.imap_host, s.imap_port))
        done, skipped = [], 0
        box = factory()
        try:
            box.login(s.imap_user, s.imap_password)
            box.select(s.imap_folder)
            typ, data = box.search(None, "UNSEEN")
            for num in (data[0] or b"").split():
                typ, parts = box.fetch(num, "(RFC822)")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                try:
                    done.append(self.import_eml(raw))
                except (EscalationError, KeyError):
                    skipped += 1
        finally:
            try:
                box.logout()
            except Exception:  # соединение уже закрыто
                pass
        return {"processed": done, "skipped": skipped}


def capitalize_first(text: str) -> str:
    """Поднимает первую букву ответа, если после «ОТВЕТ:» остался текст с маленькой буквы."""
    body = text or ""
    i = 0
    while i < len(body) and body[i] in " «\"'(":
        i += 1
    if i < len(body) and body[i].islower():
        return body[:i] + body[i].upper() + body[i + 1:]
    return body


def extract_reply(text: str) -> str:
    """Текст ответа без цитаты исходного письма и подписи."""
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if _QUOTE_START.match(line.strip()):
            break
        if line.strip() in ("--", "-- "):
            break
        out.append(line)
    return "\n".join(out).strip()


def is_final_answer(body: str) -> bool:
    if _FINAL_TAG.match(body):
        return True
    if _NOT_FINAL.match(body):
        return False
    return not body.rstrip().endswith("?")
