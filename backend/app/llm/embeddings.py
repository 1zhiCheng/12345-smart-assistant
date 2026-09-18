"""向量模型客户端。

provider:
- relay：经中转站调用 bge-m3（默认）
- local：本地 sentence-transformers（离线 fallback）
- hash ：确定性哈希向量（仅开发/测试，无网络可用）
"""
from __future__ import annotations
from typing import Optional

import hashlib
import math
import re

from app.config import Settings
from app.llm.relay import RelayClient
from app.utils.logging import get_logger

logger = get_logger(__name__)


class EmbeddingClient:
    def __init__(self, settings: Settings, relay: Optional[RelayClient] = None) -> None:
        self.settings = settings
        self.provider = settings.embedding_provider.lower()
        self.model = settings.embedding_model
        self.dim = settings.embedding_dim
        self.relay = relay
        self._local_model = None
        self.last_effective_provider: Optional[str] = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.provider == "relay":
            if self.relay is None or not self.settings.relay_api_key:
                return self._fallback_or_raise(texts, "relay 向量服务未配置 API key")
            try:
                vecs = await self.relay.embed(texts, model=self.model)
                self._validate_vectors(vecs, len(texts))
                self.dim = len(vecs[0])
                self.last_effective_provider = "relay"
                return vecs
            except Exception as exc:  # noqa: BLE001 - 向量失败不应阻断入库主流程
                return self._fallback_or_raise(texts, f"中转站向量失败: {exc}")
        if self.provider == "local":
            try:
                vecs = self._local_embed(texts)
                self._validate_vectors(vecs, len(texts))
                self.dim = len(vecs[0])
                self.last_effective_provider = "local"
                return vecs
            except Exception as exc:  # noqa: BLE001 - 是否降级由显式配置决定
                return self._fallback_or_raise(texts, f"本地向量模型失败: {exc}")
        if self.provider != "hash":
            raise RuntimeError(f"不支持的 EMBEDDING_PROVIDER: {self.provider}")
        self.last_effective_provider = "hash"
        return [self._hash_embed(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        vecs = await self.embed([text])
        return vecs[0]

    def _local_embed(self, texts: list[str]) -> list[list[float]]:
        try:
            from sentence_transformers import SentenceTransformer  # 延迟导入，可选依赖
        except ImportError as exc:
            raise RuntimeError("未安装 sentence-transformers") from exc
        if self._local_model is None:
            # 比赛运行时必须可断网复现；模型未预下载时由上层安全降级，
            # 不在请求过程中临时访问 Hugging Face。
            self._local_model = SentenceTransformer(self.model, local_files_only=True)
        vecs = self._local_model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vecs]

    def _fallback_or_raise(self, texts: list[str], reason: str) -> list[list[float]]:
        if not self.settings.embedding_allow_hash_fallback:
            raise RuntimeError(f"{reason}；生产配置禁止静默降级到 hash 向量")
        logger.warning("%s，按 EMBEDDING_ALLOW_HASH_FALLBACK=true 降级 hash 向量", reason)
        self.last_effective_provider = "hash-fallback"
        return [self._hash_embed(t) for t in texts]

    @staticmethod
    def _validate_vectors(vectors: list[list[float]], expected_count: int) -> None:
        if len(vectors) != expected_count or not vectors:
            raise RuntimeError(f"向量数量异常: expected={expected_count}, actual={len(vectors)}")
        dimension = len(vectors[0])
        if dimension <= 0 or any(len(vector) != dimension for vector in vectors):
            raise RuntimeError("向量维度为空或不一致")
        if any(not math.isfinite(value) for vector in vectors for value in vector):
            raise RuntimeError("向量包含 NaN/Inf")

    def _hash_embed(self, text: str) -> list[float]:
        """确定性哈希向量：对字符 n-gram 做 hashing 得到稀疏向量，再归一化。

        仅用于离线开发/测试，不具备真实语义，但保证 cosine 可计算且文本相似者相近。
        """
        text = self._normalize(text)
        dim = max(self.dim, 128)
        vec = [0.0] * dim
        ngrams = [text]
        if len(text) > 2:
            ngrams += [text[i : i + 2] for i in range(len(text) - 1)]
        for ng in ngrams:
            for suffix in ("", " "):
                h = hashlib.md5((ng + suffix).encode("utf-8")).digest()
                idx = int.from_bytes(h[:4], "big") % dim
                sign = 1.0 if h[4] % 2 == 0 else -1.0
                vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    @staticmethod
    def _normalize(text: str) -> str:
        text = re.sub(r"\s+", "", text)
        return text.lower()
