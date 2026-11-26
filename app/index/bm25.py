"""BM25 по тексту пунктов (в памяти, пересобирается после изменения индекса)."""
from __future__ import annotations

import threading

import numpy as np
from rank_bm25 import BM25Okapi

from app.text import tokens


class BM25Index:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ids: list[str] = []
        self.boxes: list[str] = []
        self.bm25: BM25Okapi | None = None

    def rebuild(self, rows: list[tuple[str, str, str]]) -> None:
        """rows: (point_id, box, текст для поиска)."""
        ids = [r[0] for r in rows]
        boxes = [r[1] for r in rows]
        corpus = [tokens(r[2]) or ["_"] for r in rows]
        bm25 = BM25Okapi(corpus) if corpus else None
        with self._lock:
            self.ids, self.boxes, self.bm25 = ids, boxes, bm25

    def search(self, query: str, limit: int, boxes: list[str] | None = None) -> list[tuple[str, float]]:
        with self._lock:
            bm25, ids, row_boxes = self.bm25, self.ids, self.boxes
        q = tokens(query)
        if bm25 is None or not q:
            return []
        scores = np.asarray(bm25.get_scores(q))
        if boxes:
            allowed = set(boxes)
            mask = np.array([b in allowed for b in row_boxes])
            scores = np.where(mask, scores, -1.0)
        order = np.argsort(-scores)[:limit]
        return [(ids[i], float(scores[i])) for i in order if scores[i] > 0]
