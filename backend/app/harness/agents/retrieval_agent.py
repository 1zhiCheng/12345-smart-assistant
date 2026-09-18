"""Retrieval Agent：执行混合检索（BM25 + 向量 + Rerank），返回 top-k chunks。"""
from __future__ import annotations

from typing import Any, Optional

from app.llm.embeddings import EmbeddingClient
from app.retrieval.hybrid import HybridRetriever
from app.utils.logging import get_logger

logger = get_logger(__name__)


class RetrievalAgent:
    def __init__(self, hybrid: HybridRetriever, embeddings: EmbeddingClient, store) -> None:
        self.hybrid = hybrid
        self.embeddings = embeddings
        self.store = store

    async def retrieve(self, queries: list[str], dept_ids: Optional[list[str]] = None, top_k: int = 5) -> list[dict[str, Any]]:
        """多 query × 多部门并行检索，合并去重。"""
        if not queries:
            return []
        dept_ids = dept_ids or [None]
        seen: dict[str, dict[str, Any]] = {}
        per_query_ids: list[list[str]] = []

        for query_index, query in enumerate(queries):
            # 原问题权重最高；扩展 query 用于补召回。被多个 query 共同命中的
            # 切片累积证据，避免不同 query 的单次高分互相覆盖。
            query_weight = 1.0 if query_index == 0 else 0.7
            vec = await self.embeddings.embed_query(query)
            current_query_ids: list[str] = []
            for dept_id in dept_ids:
                if dept_id == "dept_all":
                    dept_id = None
                hits = await self.hybrid.retrieve(query, vec, dept_id=dept_id)
                for h in hits:
                    id_ = h.get("id", "")
                    if id_:
                        if id_ not in current_query_ids:
                            current_query_ids.append(id_)
                        score = h.get("rerank_score", h.get("_rrf", h.get("score", 0.0)))
                        previous = seen.get(id_)
                        previous_score = (
                            previous.get("rerank_score", previous.get("_rrf", previous.get("score", 0.0)))
                            if previous else -1.0
                        )
                        if previous is None:
                            seen[id_] = {
                                **h, "multi_query_score": float(score) * query_weight,
                                "matched_queries": [query],
                            }
                        else:
                            previous["multi_query_score"] = float(previous.get("multi_query_score", 0.0)) + float(score) * query_weight
                            if query not in previous["matched_queries"]:
                                previous["matched_queries"].append(query)
                            if score > previous_score:
                                fusion_score = previous["multi_query_score"]
                                matched_queries = previous["matched_queries"]
                                previous.update(h)
                                previous["multi_query_score"] = fusion_score
                                previous["matched_queries"] = matched_queries
            per_query_ids.append(current_query_ids)

        # 检索后统一从持久化存储回填完整 chunk，避免向量库只返回 metadata
        # 导致正文、关键词和章节信息缺失；同时在返回前强制校验文档仍为 active。
        chunks: list[dict[str, Any]] = []
        for chunk_id, hit in seen.items():
            stored = await self.store.get("chunks", chunk_id)
            if not stored:
                continue
            doc = await self.store.get_document(stored.get("doc_id", ""))
            if not doc or doc.get("status") != "active":
                continue
            full = dict(hit)
            full.update(stored)
            full["id"] = chunk_id
            full["doc_title"] = doc.get("title", "")
            chunks.append(full)
        chunks.sort(
            key=lambda x: x.get("multi_query_score", x.get("rerank_score", x.get("_rrf", x.get("score", 0.0)))),
            reverse=True,
        )
        # 每个子问题至少保留一个最高位有效命中，再用融合分数填满剩余位置。
        # 这对“生育数量 + 补贴标准”等复合诉求尤为重要。
        chunks_by_id = {item["id"]: item for item in chunks}
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        for candidate_ids in per_query_ids:
            for candidate_id in candidate_ids:
                if candidate_id in chunks_by_id and candidate_id not in selected_ids:
                    selected.append(chunks_by_id[candidate_id])
                    selected_ids.add(candidate_id)
                    break
            if len(selected) >= top_k:
                return selected
        for item in chunks:
            if item["id"] not in selected_ids:
                selected.append(item)
                selected_ids.add(item["id"])
            if len(selected) >= top_k:
                break
        return selected
