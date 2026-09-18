"""基于冻结留出集之外官方文档原型的部门语义路由。"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.llm.embeddings import EmbeddingClient
from app.utils.logging import get_logger


logger = get_logger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PROTOTYPES = PROJECT_ROOT / "backend" / "resources" / "routing_prototypes.bge-small-zh-v1.5.json"


class SemanticDepartmentRouter:
    """用离线生成的部门多原型向量给诉求排序，不在运行时读取评测集。"""

    def __init__(self, embeddings: EmbeddingClient, artifact_path: str = "") -> None:
        self.embeddings = embeddings
        self.artifact_path = Path(artifact_path).expanduser() if artifact_path else DEFAULT_PROTOTYPES
        if not self.artifact_path.is_absolute():
            self.artifact_path = (PROJECT_ROOT / self.artifact_path).resolve()
        self._artifact: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return self.artifact_path.exists()

    async def rank(self, query: str, valid: set[str]) -> list[tuple[str, float]]:
        if not query.strip() or not self.enabled:
            return []
        try:
            artifact = self._load()
            vector = await self.embeddings.embed_query(query)
            expected_dim = int(artifact["metadata"]["dimension"])
            if len(vector) != expected_dim:
                logger.warning("部门语义路由向量维度不匹配：query=%s artifact=%s", len(vector), expected_dim)
                return []
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            normalized = [value / norm for value in vector]
            scores = []
            aggregation = artifact["metadata"].get("aggregation", {})
            top_n = max(1, int(aggregation.get("top_n", 3)))
            descriptor_weight = min(1.0, max(0.0, float(aggregation.get("descriptor_weight", 0.0))))
            for dept_id, prototype_group in artifact["prototypes"].items():
                documents = prototype_group.get("documents", [])
                descriptor = prototype_group.get("descriptor", [])
                if dept_id not in valid or not documents:
                    continue
                similarities = sorted(
                    (sum(left * right for left, right in zip(normalized, prototype, strict=True)) for prototype in documents),
                    reverse=True,
                )
                document_score = sum(similarities[:top_n]) / min(top_n, len(similarities))
                descriptor_score = (
                    sum(left * right for left, right in zip(normalized, descriptor, strict=True)) if descriptor else document_score
                )
                score = (1.0 - descriptor_weight) * document_score + descriptor_weight * descriptor_score
                scores.append((dept_id, float(score)))
            return sorted(scores, key=lambda item: (-item[1], item[0]))
        except Exception as exc:  # noqa: BLE001 - 语义分支必须安全降级到关键词/LLM
            logger.warning("部门语义路由不可用，回退原路由链路(%s)", exc)
            return []

    def _load(self) -> dict[str, Any]:
        if self._artifact is None:
            payload = json.loads(self.artifact_path.read_text(encoding="utf-8"))
            if payload.get("metadata", {}).get("model") != self.embeddings.model:
                raise ValueError("部门语义原型模型与当前 embedding 模型不一致")
            if not payload.get("prototypes"):
                raise ValueError("部门语义原型为空")
            self._artifact = payload
        return self._artifact
