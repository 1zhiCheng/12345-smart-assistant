"""桌面演示模式的本地 BGE 向量缓存。

Mongo 生产模式由 MongoVectorStore 持久化向量；桌面模式刻意不依赖数据库，
因此首次启动需要为官方知识库重建内存向量。该缓存只保存已脱敏的官方 Chunk
向量和完整性指纹，不保存录音、市民输入、账号或会话数据。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np


def _fingerprint(chunks: list[dict], model: str) -> str:
    digest = hashlib.sha256()
    digest.update(model.encode("utf-8"))
    for chunk in chunks:
        digest.update(str(chunk.get("_id", "")).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(chunk.get("content", "")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


async def load_or_build_desktop_vectors(embeddings, chunks: list[dict], cache_path: str | Path) -> tuple[list[list[float]], str]:
    """返回与 chunks 顺序一致的向量以及 ``hit`` / ``rebuilt`` 状态。"""
    path = Path(cache_path)
    fingerprint = _fingerprint(chunks, str(getattr(embeddings, "model", "")))
    try:
        if path.is_file():
            with np.load(path, allow_pickle=False) as payload:
                stored_fingerprint = str(payload["fingerprint"].item())
                vectors = payload["vectors"]
            if stored_fingerprint == fingerprint and len(vectors) == len(chunks) and vectors.ndim == 2 and vectors.shape[1] > 0:
                return vectors.astype(np.float32, copy=False).tolist(), "hit"
    except (OSError, ValueError, KeyError):
        # 缓存损坏或旧格式时直接从官方 Chunk 重建，不能影响受理服务启动。
        pass

    vectors = await embeddings.embed([str(chunk.get("content", "")) for chunk in chunks])
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or len(matrix) != len(chunks) or matrix.shape[1] <= 0:
        raise RuntimeError("桌面向量缓存构建失败：向量形状异常")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.npz")
    np.savez_compressed(temporary, fingerprint=np.asarray(fingerprint), vectors=matrix)
    temporary.replace(path)
    return matrix.tolist(), "rebuilt"
