"""向量存储：内存实现（开发零运维）+ Chroma（可选）。生产可切 Milvus。

接口统一：add / search / delete_by_doc。
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Optional

import numpy as np

from app.utils.logging import get_logger

logger = get_logger(__name__)


class VectorStore:
    """向量存储接口。"""

    async def add(self, id_: str, vector: list[float], metadata: dict[str, Any]) -> None: ...
    async def search(self, vector: list[float], top_k: int = 10, dept_id: Optional[str] = None) -> list[dict[str, Any]]: ...
    async def delete_by_doc(self, doc_id: str) -> None: ...
    async def count(self) -> int: ...


class MemoryVectorStore(VectorStore):
    """内存向量库：cosine 相似度（向量需已归一化；未归一化时自动归一）。"""

    def __init__(self) -> None:
        self._vecs: dict[str, list[float]] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    async def add(self, id_: str, vector: list[float], metadata: dict[str, Any]) -> None:
        self._vecs[id_] = self._normalize(vector)
        self._meta[id_] = dict(metadata)

    async def search(self, vector: list[float], top_k: int = 10, dept_id: Optional[str] = None) -> list[dict[str, Any]]:
        q = self._normalize(vector)
        scores: list[tuple[float, str]] = []
        for id_, v in self._vecs.items():
            if dept_id and self._meta[id_].get("dept_id") != dept_id:
                continue
            scores.append((self._dot(q, v), id_))
        scores.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, id_ in scores[:top_k]:
            out.append({"id": id_, "score": float(score), **self._meta[id_]})
        return out

    async def delete_by_doc(self, doc_id: str) -> None:
        for id_ in [i for i, m in self._meta.items() if m.get("doc_id") == doc_id]:
            self._vecs.pop(id_, None)
            self._meta.pop(id_, None)

    async def count(self) -> int:
        return len(self._vecs)

    @staticmethod
    def _normalize(v: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    @staticmethod
    def _dot(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


class ChromaVectorStore(VectorStore):
    """Chroma 实现（可选，需 chromadb）。"""

    def __init__(self, collection_name: str = "wuhu_12345_chunks") -> None:
        import chromadb  # 延迟导入

        self._client = chromadb.PersistentClient(path="./chroma_data")
        self._coll = self._client.get_or_create_collection(name=collection_name, metadata={"hnsw:space": "cosine"})

    async def add(self, id_: str, vector: list[float], metadata: dict[str, Any]) -> None:
        self._coll.upsert(ids=[id_], embeddings=[vector], metadatas=[metadata])

    async def search(self, vector: list[float], top_k: int = 10, dept_id: Optional[str] = None) -> list[dict[str, Any]]:
        where = {"dept_id": dept_id} if dept_id else None
        res = self._coll.query(query_embeddings=[vector], n_results=top_k, where=where)
        out = []
        ids = res.get("ids", [[]])[0]
        dists = res.get("distances", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        for id_, dist, meta in zip(ids, dists, metas):
            out.append({"id": id_, "score": float(1.0 - dist), **(meta or {})})
        return out

    async def delete_by_doc(self, doc_id: str) -> None:
        self._coll.delete(where={"doc_id": doc_id})

    async def count(self) -> int:
        return self._coll.count()


class MongoVectorStore(VectorStore):
    """MongoDB 共享向量存储。

    适用于当前 MVP/中小数据量，所有部门 Pod 共享同一份向量与版本状态；
    超过百万 chunk 时可无缝替换为 Milvus 实现。
    """

    def __init__(
        self,
        store,
        embedding_provider: str = "unknown",
        embedding_model: str = "unknown",
        cache_ttl_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self._rows_cache: Optional[list[dict[str, Any]]] = None
        self._matrix_by_dim: dict[int, tuple[list[dict[str, Any]], np.ndarray]] = {}
        self._cache_loaded_at = 0.0
        self._cache_lock = asyncio.Lock()

    async def add(self, id_: str, vector: list[float], metadata: dict[str, Any]) -> None:
        if not vector:
            raise ValueError("不能写入空向量")
        await self.store.upsert("vector_embeddings", {
            "_id": id_,
            "vector": self._normalize(vector),
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_dim": len(vector),
            **metadata,
        })
        # 本进程立即失效；其他 Pod 最迟在 TTL 后读取 Mongo 新快照。
        self._rows_cache = None
        self._matrix_by_dim = {}

    async def search(self, vector: list[float], top_k: int = 10, dept_id: Optional[str] = None) -> list[dict[str, Any]]:
        normalized = self._normalize(vector)
        all_rows = await self._snapshot()
        compatible, matrix = self._matrix_by_dim.get(
            len(normalized), ([], np.empty((0, len(normalized)), dtype=np.float32))
        )
        skipped = len(all_rows) - len(compatible)
        if skipped:
            logger.warning("跳过 %d 条维度不匹配的历史向量；请执行重建索引", skipped)
        if dept_id:
            indices = [index for index, row in enumerate(compatible) if row.get("dept_id") == dept_id]
        else:
            indices = list(range(len(compatible)))
        if not indices:
            return []
        selected = matrix[indices]
        scores = selected @ np.asarray(normalized, dtype=np.float32)
        order = np.argsort(scores)[::-1][:top_k]
        return [
            {
                "id": compatible[indices[int(position)]]["_id"],
                "score": float(scores[int(position)]),
                **{
                    key: value
                    for key, value in compatible[indices[int(position)]].items()
                    if key not in {"_id", "vector"}
                },
            }
            for position in order
        ]

    async def delete_by_doc(self, doc_id: str) -> None:
        for row in await self.store.find("vector_embeddings", {"doc_id": doc_id}):
            await self.store.delete("vector_embeddings", row["_id"])
        self._rows_cache = None
        self._matrix_by_dim = {}

    async def count(self) -> int:
        return await self.store.count("vector_embeddings")

    async def _snapshot(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._rows_cache is not None and now - self._cache_loaded_at < self.cache_ttl_seconds:
            return self._rows_cache
        async with self._cache_lock:
            now = time.monotonic()
            if self._rows_cache is None or now - self._cache_loaded_at >= self.cache_ttl_seconds:
                self._rows_cache = await self.store.find("vector_embeddings")
                grouped: dict[int, list[dict[str, Any]]] = {}
                for row in self._rows_cache:
                    dimension = len(row.get("vector") or [])
                    if dimension:
                        grouped.setdefault(dimension, []).append(row)
                self._matrix_by_dim = {
                    dimension: (
                        rows,
                        np.asarray([row["vector"] for row in rows], dtype=np.float32),
                    )
                    for dimension, rows in grouped.items()
                }
                self._cache_loaded_at = now
        return self._rows_cache

    @staticmethod
    def _normalize(v: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    @staticmethod
    def _dot(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


def build_vector_store(
    backend: str,
    store=None,
    embedding_provider: str = "unknown",
    embedding_model: str = "unknown",
    cache_ttl_seconds: float = 30.0,
) -> VectorStore:
    if backend == "mongo" and store is not None:
        return MongoVectorStore(store, embedding_provider, embedding_model, cache_ttl_seconds)
    if backend == "chroma":
        try:
            return ChromaVectorStore()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chroma 初始化失败(%s)，回退内存向量库", exc)
    return MemoryVectorStore()
