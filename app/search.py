"""Гибридный поиск: вектор (e5) + BM25 → слияние RRF → реранкер (0..1) → поправка памяти отзывов."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field

import numpy as np

from app.boxes import GENERAL_BOX, Route
from app.index.indexer import search_text
from app.parsing import format_ref

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    point_id: str
    doc_id: str
    doc_title: str
    box: str
    clause_no: str
    section: str
    page: int | None
    kind: str
    text: str
    score: float = 0.0  # итоговая оценка 0..1
    rerank_score: float = 0.0  # оценка реранкера 0..1 до поправки памяти
    memory: float = 0.0  # чистый вклад памяти (да − нет)

    @property
    def ref(self) -> str:
        return format_ref(self.doc_title, self.clause_no, self.page, self.kind)

    @property
    def key(self) -> tuple[str, str]:
        return self.doc_id, self.clause_no

    def public(self) -> dict:
        d = asdict(self)
        d["ref"] = self.ref
        return d


@dataclass
class SearchResult:
    question: str
    q_vec: np.ndarray | None
    route: Route
    candidates: list[Candidate] = field(default_factory=list)
    searched_all: bool = False
    # Сколько нашёл каждый инструмент поиска и сколько это заняло (для трассы агента)
    stats: dict = field(default_factory=dict)

    @property
    def top(self) -> float:
        return self.candidates[0].score if self.candidates else 0.0


class Searcher:
    def __init__(self, settings, db, store, embedder, reranker, bm25, boxes, memory, llm=None) -> None:
        self.settings = settings
        self.db = db
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.bm25 = bm25
        self.boxes = boxes
        self.memory = memory
        self.llm = llm
        # Реранкер тяжёлый (секунды на CPU): параллельные реранки только замедляют друг друга
        self._rerank_slots = threading.BoundedSemaphore(max(1, getattr(settings, "llm_max_concurrency", 2)))

    def search(self, question: str, boxes: list[str] | None = None, strict: bool = False,
               route: bool = True) -> SearchResult:
        """strict=True — искать только в заданных ящичках (подразделение выбрано), без расширения на все;
        route=False — не угадывать направление, искать по всем документам."""
        stats = {"vector": 0, "bm25": 0, "fused": 0, "reranked": 0, "memory_adjusted": 0,
                 "retrieve_ms": 0, "rerank_ms": 0, "route_ms": 0}
        q_vec = self.embedder.embed_queries([question])[0]
        t = time.perf_counter()
        use_router = route
        if boxes:
            route = Route([b for b in boxes if b in self.boxes.boxes], {}, "manual")
        elif not use_router:
            route = Route([], {}, "none")
        else:
            route = self.boxes.route(question, q_vec, self.embedder, self.settings, self.llm)
        stats["route_ms"] = int((time.perf_counter() - t) * 1000)
        adjustments = self.memory.adjustments(q_vec)
        cands = self._retrieve(question, q_vec, route.boxes, adjustments, stats)
        searched_all = not route.boxes
        if route.boxes and not strict and (not cands or cands[0].score < self.settings.box_fallback_threshold):
            more = self._retrieve(question, q_vec, None, adjustments, stats)
            by_id = {c.point_id: c for c in cands}
            for c in more:
                by_id.setdefault(c.point_id, c)
            cands = sorted(by_id.values(), key=lambda c: -c.score)
            searched_all = True
        stats["memory_adjusted"] = sum(1 for c in cands if c.memory)
        return SearchResult(question, q_vec, route, cands, searched_all, stats)

    def _retrieve(self, question: str, q_vec: np.ndarray, boxes: list[str] | None, adjustments,
                  stats: dict | None = None) -> list[Candidate]:
        s = self.settings
        stats = stats if stats is not None else {}
        t = time.perf_counter()
        flt = (list(boxes) + [GENERAL_BOX]) if boxes else None
        vec_hits = self.store.search(q_vec, s.vector_top_k, flt)
        bm_hits = self.bm25.search(question, s.bm25_top_k, flt)
        stats["vector"] = stats.get("vector", 0) + len(vec_hits)
        stats["bm25"] = stats.get("bm25", 0) + len(bm_hits)
        fused: dict[str, float] = {}
        for hits in (vec_hits, bm_hits):
            for rank, (pid, _) in enumerate(hits):
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (s.rrf_k + rank + 1)
        ids = [pid for pid, _ in sorted(fused.items(), key=lambda x: -x[1])[: s.rerank_top_n]]

        # пункты, подтверждённые в похожих вопросах, проверяем всегда — даже если поиск их не поднял
        confirmed = [k for k, a in adjustments.items() if a.net > 0]
        if confirmed:
            for doc_id, clause_no in confirmed:
                for r in self.db.query("SELECT point_id FROM clauses WHERE doc_id=? AND clause_no=?",
                                       (doc_id, clause_no)):
                    if r["point_id"] not in ids:
                        ids.append(r["point_id"])
        cands = self._load(ids)
        stats["fused"] = stats.get("fused", 0) + len(ids)
        stats["retrieve_ms"] = stats.get("retrieve_ms", 0) + int((time.perf_counter() - t) * 1000)
        if not cands:
            return []
        t = time.perf_counter()
        with self._rerank_slots:
            scores = self.reranker.score(
                question, [search_text(c.doc_title, c.section, c.clause_no, c.text, c.kind) for c in cands]
            )
        stats["reranked"] = stats.get("reranked", 0) + len(cands)
        stats["rerank_ms"] = stats.get("rerank_ms", 0) + int((time.perf_counter() - t) * 1000)
        for c, sc in zip(cands, scores):
            c.rerank_score = float(min(1.0, max(0.0, sc)))
            adj = adjustments.get(c.key)
            c.memory = adj.net if adj else 0.0
            c.score = self.memory.apply(c.rerank_score, adj)
        cands.sort(key=lambda c: -c.score)
        return cands

    def _load(self, ids: list[str]) -> list[Candidate]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.db.query(
            "SELECT c.point_id, c.doc_id, f.title, f.box, c.clause_no, c.section, c.page, c.kind, c.text "
            f"FROM clauses c JOIN files f ON f.doc_id = c.doc_id WHERE c.point_id IN ({marks})", tuple(ids))
        by_id = {r["point_id"]: r for r in rows}
        out = []
        for pid in ids:
            r = by_id.get(pid)
            if r is None:
                continue  # точка осталась в Qdrant, но документа уже нет — не показываем
            out.append(Candidate(pid, r["doc_id"], r["title"], r["box"], r["clause_no"], r["section"], r["page"],
                                 r["kind"], r["text"]))
        return out
