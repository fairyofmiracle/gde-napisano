"""Эмбеддинги (e5) и реранкер (cross-encoder) плюс простые заглушки для тестов."""
from __future__ import annotations

import hashlib
import logging
import math
import threading
from collections import OrderedDict

import numpy as np

from app.text import tokens

log = logging.getLogger(__name__)


def sigmoid(x: float) -> float:
    # устойчиво для больших |x|; результат всегда в [0, 1]
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class LRUCache:
    """Небольшой потокобезопасный кэш: одинаковый вопрос и пункт дают одну и ту же оценку."""

    def __init__(self, size: int) -> None:
        self.size = max(0, int(size))
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        if not self.size:
            return None
        with self._lock:
            v = self._data.get(key)
            if v is None:
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return v

    def put(self, key, value) -> None:
        if not self.size:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class E5Embedder:
    """intfloat/multilingual-e5-*: префиксы «query: » и «passage: » обязательны."""

    def __init__(self, model_name: str, device: str = "cpu", cache_size: int = 512) -> None:
        self.model_name = model_name
        self.device = device
        self._model = None
        self._lock = threading.Lock()
        self.query_cache = LRUCache(cache_size)

    @property
    def model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    log.info("Загружаю модель эмбеддингов %s", self.model_name)
                    self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    @property
    def dim(self) -> int:
        getter = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        return int(getter())

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return self.model.encode([f"passage: {t}" for t in texts], normalize_embeddings=True,
                                 batch_size=16, convert_to_numpy=True).astype(np.float32)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        cached = [self.query_cache.get(t) for t in texts]
        todo = [t for t, v in zip(texts, cached) if v is None]
        if todo:
            vecs = self.model.encode([f"query: {t}" for t in todo], normalize_embeddings=True,
                                     convert_to_numpy=True).astype(np.float32)
            fresh = dict(zip(todo, vecs))
            for t, v in fresh.items():
                self.query_cache.put(t, v)
            cached = [v if v is not None else fresh[t] for t, v in zip(texts, cached)]
        return np.stack(cached).astype(np.float32) if cached else np.zeros((0, self.dim), dtype=np.float32)


class FakeEmbedder:
    """Детерминированные «эмбеддинги» по основам слов — только для тестов."""

    dim = 256

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for t in tokens(text):
            h = int(hashlib.md5(t.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return np.array([self._vec(t) for t in texts], dtype=np.float32).reshape(len(texts), self.dim)

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self.embed_passages(texts)


class CrossEncoderReranker:
    """BAAI/bge-reranker-v2-m3: сырой логит → сигмоида, оценка строго в [0, 1]."""

    def __init__(self, model_name: str, device: str = "cpu", max_length: int = 512, cache_size: int = 4096,
                 batch_size: int = 4) -> None:
        self.model_name = model_name
        self.device = device
        self.max_length = max_length
        self._model = None
        self._lock = threading.Lock()
        # Оценка пары «вопрос — пункт» не зависит от остальных пар: повторный поиск по тому же вопросу
        # (уточнение подразделения, ответ по подразделениям, повтор вопроса) не пересчитывает реранкер.
        self.cache = LRUCache(cache_size)
        self.batch_size = max(1, int(batch_size))

    @property
    def model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    import torch
                    from sentence_transformers import CrossEncoder

                    log.info("Загружаю реранкер %s", self.model_name)
                    self._model = CrossEncoder(self.model_name, device=self.device, max_length=self.max_length,
                                               activation_fn=torch.nn.Identity())
        return self._model

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        import torch

        out = [self.cache.get((query, p)) for p in passages]
        todo = sorted({p for p, v in zip(passages, out) if v is None}, key=len)
        if todo:
            # короткие пункты вместе: меньше дополнения до длины самого длинного в пачке, оценки те же
            logits = self.model.predict([(query, p) for p in todo], batch_size=self.batch_size,
                                        activation_fn=torch.nn.Identity(), show_progress_bar=False)
            fresh = {p: min(1.0, max(0.0, sigmoid(float(x)))) for p, x in zip(todo, np.asarray(logits).reshape(-1))}
            for p, v in fresh.items():
                self.cache.put((query, p), v)
            out = [v if v is not None else fresh[p] for p, v in zip(passages, out)]
        return out


class FakeReranker:
    """Доля основ слов вопроса, найденных в пункте, через сигмоиду — только для тестов."""

    def score(self, query: str, passages: list[str]) -> list[float]:
        q = set(tokens(query))
        out = []
        for p in passages:
            ratio = len(q & set(tokens(p))) / max(len(q), 1)
            out.append(sigmoid(10 * (ratio - 0.5)))
        return out


def make_embedder(settings):
    if settings.embed_provider == "fake":
        return FakeEmbedder()
    return E5Embedder(settings.embed_model, settings.model_device, getattr(settings, "query_cache_size", 512))


def make_reranker(settings):
    if settings.reranker_provider == "fake":
        return FakeReranker()
    return CrossEncoderReranker(settings.reranker_model, settings.model_device, settings.reranker_max_length,
                                getattr(settings, "rerank_cache_size", 4096), getattr(settings, "rerank_batch_size", 4))
