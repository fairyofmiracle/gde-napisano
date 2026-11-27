"""Кэш реранкера и эмбеддингов вопроса: те же оценки, без повторного счёта."""
from __future__ import annotations

import numpy as np

from app.index.models import CrossEncoderReranker, E5Embedder, LRUCache, sigmoid


class _CE:
    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs, batch_size=8, activation_fn=None, show_progress_bar=False):
        self.calls.append(list(pairs))
        return np.array([float(len(p)) / 10 - 2 for _, p in pairs])


class _ST:
    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True, batch_size=32):
        self.calls += 1
        return np.array([[len(t), 1.0] for t in texts], dtype=np.float32)


def _rr(size=16):
    rr = CrossEncoderReranker("m", cache_size=size)
    rr._model = _CE()
    return rr


def test_scores_keep_input_order_and_match_uncached():
    rr = _rr()
    ps = ["пункт длинный текст тут", "кор", "средний пункт"]
    got = rr.score("вопрос", ps)
    assert got == [sigmoid(len(p) / 10 - 2) for p in ps]
    assert [p for _, p in rr._model.calls[0]] == sorted(ps, key=len)  # короткие пункты в пачке рядом


def test_repeated_pairs_are_not_rescored():
    rr = _rr()
    first = rr.score("вопрос", ["a1", "b22", "c333"])
    again = rr.score("вопрос", ["c333", "a1", "d4444"])
    assert again[:2] == [first[2], first[0]]
    assert len(rr._model.calls) == 2 and [p for _, p in rr._model.calls[1]] == ["d4444"]
    rr.score("вопрос", ["a1", "c333"])
    assert len(rr._model.calls) == 2
    rr.score("другой вопрос", ["a1"])
    assert len(rr._model.calls) == 3


def test_duplicate_passages_scored_once():
    rr = _rr()
    out = rr.score("q", ["same", "same", "x"])
    assert out[0] == out[1] and len(rr._model.calls[0]) == 2


def test_cache_disabled_and_bounded():
    rr = _rr(size=0)
    rr.score("q", ["a"])
    rr.score("q", ["a"])
    assert len(rr._model.calls) == 2
    c = LRUCache(2)
    for k in "abc":
        c.put(k, 1)
    assert c.get("a") is None and c.get("c") == 1


def test_query_embeddings_cached():
    e = E5Embedder("m", cache_size=8)
    e._model = _ST()
    v1 = e.embed_queries(["вопрос"])
    v2 = e.embed_queries(["вопрос"])
    assert e._model.calls == 1 and np.array_equal(v1, v2) and v1.shape == (1, 2)
    e.embed_queries(["вопрос", "новый"])
    assert e._model.calls == 2
