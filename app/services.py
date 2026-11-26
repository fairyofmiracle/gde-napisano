"""Сборка всех компонентов приложения."""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass

from app.answer import AnswerService
from app.boxes import BoxDirectory
from app.db import Database
from app.index.bm25 import BM25Index
from app.index.indexer import Indexer
from app.index.models import make_embedder, make_reranker
from app.index.store import VectorStore
from app.llm import make_llm
from app.memory import Memory
from app.search import Searcher
from app.security import RateLimiter

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: object
    db: Database
    boxes: BoxDirectory
    embedder: object
    reranker: object
    store: VectorStore
    bm25: BM25Index
    indexer: Indexer
    memory: Memory
    llm: object
    searcher: Searcher
    answers: AnswerService
    limiter: RateLimiter

    def close(self) -> None:
        close_llm = getattr(self.llm, "close", None)
        if callable(close_llm):
            close_llm()
        self.store.close()
        self.db.close()


def build_services(settings, llm=None, embedder=None, reranker=None, rng: random.Random | None = None) -> Services:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    boxes = BoxDirectory.load(settings.boxes_csv)
    embedder = embedder or make_embedder(settings)
    reranker = reranker or make_reranker(settings)
    llm = llm or make_llm(settings)  # без ключа GigaChat здесь будет явная ошибка
    store = VectorStore(settings, embedder.dim)
    bm25 = BM25Index()
    indexer = Indexer(settings, db, store, embedder, bm25, boxes)
    indexer.prepare()
    memory = Memory(db, settings)
    searcher = Searcher(settings, db, store, embedder, reranker, bm25, boxes, memory, llm)
    searcher.indexer = indexer
    answers = AnswerService(settings, searcher, llm, memory, boxes, rng)
    limiter = RateLimiter(settings.rate_limit_per_minute, settings.redis_url)
    return Services(settings, db, boxes, embedder, reranker, store, bm25, indexer, memory, llm, searcher, answers,
                    limiter)
