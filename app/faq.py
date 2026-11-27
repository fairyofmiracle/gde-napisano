"""База разъяснений службы качества (FAQ): ответы экспертов на вопросы, которых нет в регламентах.

Разъяснение привязано к исходному вопросу и, если был найден, к пункту регламента. Хранится отдельно
от регламентов (их бот не меняет) и ищется по смыслу вопроса до поиска по регламентам.
"""
from __future__ import annotations

import threading

import numpy as np

from app.memory import from_blob, to_blob
from app.text import tokens


def overlap(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


class FAQ:
    def __init__(self, db, settings, embedder) -> None:
        self.db = db
        self.settings = settings
        self.embedder = embedder
        self._lock = threading.Lock()
        self._cache = None

    def add(self, question: str, answer: str, *, doc_id: str = "", doc_title: str = "", clause_no: str = "",
            division: str = "", expert: str = "", escalation_id: int | None = None, source: str = "expert") -> int:
        vec = self.embedder.embed_queries([question])[0]
        cur = self.db.execute(
            "INSERT INTO faq(question, answer, q_vec, doc_id, doc_title, clause_no, division, source, expert,"
            " escalation_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (question, answer, to_blob(vec), doc_id, doc_title, clause_no, division, source, expert, escalation_id))
        with self._lock:
            self._cache = None
        return int(cur.lastrowid)

    def _load(self):
        with self._lock:
            if self._cache is None:
                rows = self.db.query("SELECT * FROM faq WHERE active=1 AND q_vec IS NOT NULL")
                vecs = [from_blob(r["q_vec"]) for r in rows]
                rows = [r for r, v in zip(rows, vecs) if v is not None and v.size]
                vecs = [v for v in vecs if v is not None and v.size]
                mat = np.stack(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)
                self._cache = (mat, rows)
            return self._cache

    def lookup(self, question: str, q_vec=None, division: str = "") -> dict | None:
        mat, rows = self._load()
        if not rows:
            return None
        if q_vec is None:
            q_vec = self.embedder.embed_queries([question])[0]
        if mat.shape[1] != q_vec.size:
            return None
        sims = mat @ q_vec
        s = self.settings
        best, best_i = None, -1
        for i in np.argsort(-sims)[:5]:
            r, sim = rows[int(i)], float(sims[int(i)])
            if division and r["division"] and r["division"] != division:
                continue
            ov = overlap(question, r["question"])
            if sim >= s.faq_similarity or (sim >= s.faq_similarity_soft and ov >= s.faq_min_overlap):
                best, best_i = (r, sim, ov), int(i)
                break
        if best is None:
            return None
        r, sim, ov = best
        self.db.execute("UPDATE faq SET hits=hits+1, last_hit_at=datetime('now') WHERE id=?", (r["id"],))
        hits = int(self.db.query("SELECT hits FROM faq WHERE id=?", (r["id"],))[0]["hits"])
        return {"id": r["id"], "question": r["question"], "answer": r["answer"], "created_at": r["created_at"],
                "doc_id": r["doc_id"], "doc_title": r["doc_title"], "clause_no": r["clause_no"],
                "division": r["division"], "expert": r["expert"], "escalation_id": r["escalation_id"],
                "similarity": round(sim, 3), "hits": hits,
                "review_suggested": hits >= max(1, s.review_repeat_threshold)}

    def list(self, limit: int = 200) -> list[dict]:
        return [{k: r[k] for k in ("id", "created_at", "question", "answer", "doc_title", "clause_no", "division",
                                   "expert", "escalation_id", "hits", "last_hit_at", "active")}
                for r in self.db.query("SELECT * FROM faq ORDER BY id DESC LIMIT ?", (limit,))]
