"""Qdrant: сервер (QDRANT_URL) или локальная папка без сервера (QDRANT_PATH)."""
from __future__ import annotations

import logging

import numpy as np
from qdrant_client import QdrantClient, models

log = logging.getLogger(__name__)


class VectorStore:
    def __init__(self, settings, dim: int) -> None:
        if settings.qdrant_url:
            self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None, timeout=30)
            self.local = False
        elif settings.qdrant_path == ":memory:":
            self.client = QdrantClient(location=":memory:")
            self.local = True
        else:
            self.client = QdrantClient(path=settings.qdrant_path)
            self.local = True
        self.collection = settings.qdrant_collection
        self.dim = dim

    def ensure(self, recreate: bool = False) -> bool:
        """Создать коллекцию. Возвращает True, если коллекция создана заново (индекс пуст)."""
        exists = self.client.collection_exists(self.collection)
        if exists and not recreate:
            info = self.client.get_collection(self.collection)
            size = info.config.params.vectors.size
            if size == self.dim:
                return False
            log.warning("Размерность коллекции %s ≠ %s — пересоздаю", size, self.dim)
        if exists:
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            self.collection, vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE)
        )
        if not self.local:
            for field in ("box", "doc_id"):
                self.client.create_payload_index(self.collection, field, models.PayloadSchemaType.KEYWORD)
        return True

    def upsert(self, ids: list[str], vectors: np.ndarray, payloads: list[dict]) -> None:
        if not ids:
            return
        points = [models.PointStruct(id=i, vector=v.tolist(), payload=p) for i, v, p in zip(ids, vectors, payloads)]
        for start in range(0, len(points), 256):
            self.client.upsert(self.collection, points=points[start : start + 256], wait=True)

    def delete(self, ids: list[str]) -> None:
        if ids:
            self.client.delete(self.collection, points_selector=models.PointIdsList(points=list(ids)), wait=True)

    def set_box(self, doc_id: str, box: str) -> None:
        self.client.set_payload(
            self.collection, payload={"box": box},
            points=models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]),
            wait=True,
        )

    def count(self) -> int:
        return self.client.count(self.collection, exact=True).count

    def search(self, vector: np.ndarray, limit: int, boxes: list[str] | None = None) -> list[tuple[str, float]]:
        flt = None
        if boxes:
            flt = models.Filter(must=[models.FieldCondition(key="box", match=models.MatchAny(any=list(boxes)))])
        res = self.client.query_points(self.collection, query=vector.tolist(), limit=limit, query_filter=flt,
                                       with_payload=False)
        return [(str(p.id), float(p.score)) for p in res.points]

    def healthy(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
