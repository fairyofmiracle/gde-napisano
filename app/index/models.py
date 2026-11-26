"""Эмбеддинги (e5) и реранкер (cross-encoder) плюс простые заглушки для тестов."""
from __future__ import annotations

import hashlib
import logging
import math
import threading

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


class E5Embedder:
    """intfloat/multilingual-e5-*: префиксы «query: » и «passage: » обязательны."""

    def __init__(self, model_name: str, device: str = "cpu") -> None:
        self.model_name = model_name
        self.device = device
        self._model = None
        self._lock = threading.Lock()

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
        return self.model.encode([f"query: {t}" for t in texts], normalize_embeddings=True,
                                 convert_to_numpy=True).astype(np.float32)


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

    def __init__(self, model_name: str, device: str = "cpu", max_length: int = 512) -> None:
        self.model_name = model_name
        self.device = device
        self.max_length = max_length
        self._model = None
        self._lock = threading.Lock()

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

        logits = self.model.predict([(query, p) for p in passages], batch_size=8,
                                    activation_fn=torch.nn.Identity(), show_progress_bar=False)
        return [min(1.0, max(0.0, sigmoid(float(x)))) for x in np.asarray(logits).reshape(-1)]


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
    return E5Embedder(settings.embed_model, settings.model_device)


def make_reranker(settings):
    if settings.reranker_provider == "fake":
        return FakeReranker()
    return CrossEncoderReranker(settings.reranker_model, settings.model_device, settings.reranker_max_length)
