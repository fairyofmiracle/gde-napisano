"""Память отзывов «да/нет» и практика применения пунктов. Модель не дообучается:
подтверждённые пары «вопрос → пункт» поднимают пункт для похожих вопросов, отклонённые — опускают."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass

import numpy as np

from app.db import Database


def to_blob(v: np.ndarray | None) -> bytes | None:
    return None if v is None else np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b: bytes | None) -> np.ndarray | None:
    return None if b is None else np.frombuffer(b, dtype=np.float32)


@dataclass
class Adjustment:
    yes: float = 0.0
    no: float = 0.0

    @property
    def net(self) -> float:
        return self.yes - self.no


class Memory:
    def __init__(self, db: Database, settings) -> None:
        self.db = db
        self.settings = settings
        self._lock = threading.Lock()
        self._cache: tuple[np.ndarray, list[tuple[str, str, int]]] | None = None

    # --- ответы ------------------------------------------------------------------------
    def record_answer(self, answer_id: str, question: str, q_vec, boxes: list[str], mode: str, top: float | None,
                      candidates: list[dict], provider: str, prompt_tokens: int, completion_tokens: int,
                      latency_ms: int) -> None:
        self.db.execute(
            "INSERT INTO answers(answer_id, question, q_vec, boxes, mode, top_score, candidates, llm_provider,"
            " prompt_tokens, completion_tokens, latency_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, question, to_blob(q_vec), ",".join(boxes), mode, top, json.dumps(candidates, ensure_ascii=False),
             provider, prompt_tokens, completion_tokens, latency_ms),
        )

    def get_answer(self, answer_id: str):
        rows = self.db.query("SELECT * FROM answers WHERE answer_id=?", (answer_id,))
        return rows[0] if rows else None

    # --- отзывы ------------------------------------------------------------------------
    def add_feedback(self, answer_id: str, helpful: bool, doc_id: str = "", doc_title: str = "",
                     clause_no: str = "", client: str = "") -> bool:
        """Один голос на пару (ответ, пункт): повторный голос заменяет прежний, а не складывается.
        Возвращает True, если голос новый."""
        ans = self.get_answer(answer_id)
        if ans is None:
            raise KeyError(answer_id)
        existed = self.db.query("SELECT 1 FROM feedback WHERE answer_id=? AND doc_id=? AND clause_no=?",
                                (answer_id, doc_id, clause_no))
        self.db.execute(
            "INSERT INTO feedback(answer_id, question, q_vec, doc_id, doc_title, clause_no, helpful, client)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(answer_id, doc_id, clause_no) DO UPDATE SET"
            " helpful=excluded.helpful, client=excluded.client, created_at=datetime('now')",
            (answer_id, ans["question"], ans["q_vec"], doc_id, doc_title, clause_no, int(helpful), client),
        )
        with self._lock:
            self._cache = None
        return not existed

    def _load(self) -> tuple[np.ndarray, list[tuple[str, str, int, str]]]:
        with self._lock:
            if self._cache is None:
                rows = self.db.query(
                    "SELECT q_vec, doc_id, clause_no, helpful, client FROM feedback"
                    " WHERE q_vec IS NOT NULL AND doc_id != ''"
                )
                vecs = [from_blob(r["q_vec"]) for r in rows]
                meta = [(r["doc_id"], r["clause_no"], r["helpful"], r["client"] or "") for r in rows]
                dim_ok = [i for i, v in enumerate(vecs) if v is not None and v.size]
                if dim_ok:
                    size = vecs[dim_ok[0]].size
                    dim_ok = [i for i in dim_ok if vecs[i].size == size]
                mat = np.stack([vecs[i] for i in dim_ok]) if dim_ok else np.zeros((0, 1), dtype=np.float32)
                self._cache = (mat, [meta[i] for i in dim_ok])
            return self._cache

    def adjustments(self, q_vec: np.ndarray | None) -> dict[tuple[str, str], Adjustment]:
        if q_vec is None:
            return {}
        mat, meta = self._load()
        if not meta or mat.shape[1] != q_vec.size:
            return {}
        sims = mat @ q_vec
        thr = self.settings.memory_similarity
        # Один адрес = один голос на пункт: берём самый весомый голос каждого клиента по каждому знаку.
        # Так сотня отзывов с одного адреса весит как один.
        best: dict[tuple[str, str, str, int], float] = {}
        for sim, (doc_id, clause_no, helpful, client) in zip(sims, meta):
            if sim < thr:
                continue
            w = float((sim - thr) / max(1e-6, 1 - thr)) * 0.5 + 0.5  # 0.5..1 в зависимости от похожести
            k = (doc_id, clause_no, client, int(bool(helpful)))
            best[k] = max(best.get(k, 0.0), w)
        out: dict[tuple[str, str], Adjustment] = {}
        for (doc_id, clause_no, _client, helpful), w in best.items():
            adj = out.setdefault((doc_id, clause_no), Adjustment())
            if helpful:
                adj.yes += w
            else:
                adj.no += w
        return out

    def apply(self, score: float, adj: Adjustment | None) -> float:
        """Сдвиг оценки с сохранением диапазона [0, 1]. Подъём ограничен MEMORY_MAX_BOOST:
        отзывы помогают выбрать между близкими пунктами, но не делают нерелевантный пункт «уверенным»."""
        if adj is None or adj.net == 0:
            return score
        if adj.net > 0:
            raised = score + (1 - score) * self.settings.memory_boost * min(1.0, adj.net)
            score = min(raised, score + max(0.0, getattr(self.settings, "memory_max_boost", 1.0)))
        else:
            score = score * (1 - self.settings.memory_penalty * min(1.0, -adj.net))
        return min(1.0, max(0.0, score))

    # --- практика ----------------------------------------------------------------------
    def add_practice(self, answer_id: str, text: str, doc_id: str = "", doc_title: str = "", clause_no: str = "",
                     box: str = "") -> int:
        ans = self.get_answer(answer_id)
        if ans is None:
            raise KeyError(answer_id)
        cur = self.db.execute(
            "INSERT INTO practice(answer_id, question, q_vec, doc_id, doc_title, clause_no, box, text)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (answer_id, ans["question"], ans["q_vec"], doc_id, doc_title, clause_no, box, text.strip()),
        )
        return int(cur.lastrowid)

    def _visible(self) -> str:
        return "status = 'approved'" if self.settings.practice_requires_approval else "status != 'hidden'"

    def practice_for(self, doc_id: str, clause_no: str, limit: int = 2) -> list[dict]:
        rows = self.db.query(
            "SELECT id, question, text, created_at FROM practice WHERE doc_id=? AND clause_no=? AND "
            f"{self._visible()} ORDER BY id DESC LIMIT ?", (doc_id, clause_no, limit))
        return [dict(r) for r in rows]

    def similar_practice(self, q_vec: np.ndarray | None, limit: int = 2) -> list[dict]:
        if q_vec is None:
            return []
        rows = self.db.query(
            "SELECT id, question, text, created_at, q_vec FROM practice WHERE q_vec IS NOT NULL AND "
            f"{self._visible()} ORDER BY id DESC LIMIT 2000")
        scored = []
        for r in rows:
            v = from_blob(r["q_vec"])
            if v is not None and v.size == q_vec.size:
                sim = float(v @ q_vec)
                if sim >= self.settings.memory_similarity:
                    scored.append((sim, {k: r[k] for k in ("id", "question", "text", "created_at")}))
        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:limit]]
