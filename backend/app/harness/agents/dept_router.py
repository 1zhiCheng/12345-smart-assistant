"""Dept Router（自动部门路由 Agent）：把 12345 群众诉求匹配到承办部门。

策略：关键词精确匹配（快速、可解释）→ LLM 语义路由（兜底）→ 全部部门。
返回路由结果（dept_ids / matched_by / confidence / reasons / dept_names），
供前端展示"已自动路由到 XX 部门"。
"""
from __future__ import annotations

from typing import Any

from app.llm.client import ChatMessage, LLMClient
from app.storage.store import DataStore
from app.utils.logging import get_logger
from app.domain.wuhu import DEPARTMENT_KEYWORDS

logger = get_logger(__name__)

# 部门关键词路由表（覆盖全部部门；命中即路由到对应部门，可多部门）
DEPT_KEYWORDS = DEPARTMENT_KEYWORDS

ROUTE_PROMPT = """你是芜湖 12345 事项分类与部门路由助手。判断群众诉求应由哪个部门承办，输出 JSON：
{{"depts": ["dept_id"], "confidence": 0.0-1.0, "reason": "简短理由"}}

候选部门：
{departments}

群众诉求：{query}
"""


class DeptRouter:
    """自动部门路由：群众诉求 → 最匹配的承办部门。"""

    def __init__(self, llm: LLMClient, store: DataStore) -> None:
        self.llm = llm
        self.store = store

    async def route(self, query: str) -> dict[str, Any]:
        departments = await self.store.list_departments()
        dept_names = {d["_id"]: d.get("name", d["_id"]) for d in departments}
        valid = set(dept_names)

        # 1) 关键词精确匹配（快速、可解释）
        matched, reasons = self._keyword_match(query, valid)
        if matched:
            return self._result(matched, "keyword", min(0.95, 0.6 + 0.1 * len(matched)), reasons, dept_names)

        # 2) LLM 语义路由（关键词未命中时）
        try:
            dept_desc = ", ".join(f"{d['_id']}({d.get('name', '')})" for d in departments) or "dept_all(通用)"
            data = await self.llm.complete_json(
                [ChatMessage.system("你是部门路由助手。"), ChatMessage.user(ROUTE_PROMPT.format(departments=dept_desc, query=query))],
                temperature=0.0,
            )
            depts = [d for d in (data.get("depts") or []) if d in valid]
            if depts:
                return self._result(depts, "llm", float(data.get("confidence", 0.7)), [data.get("reason", "")], dept_names)
        except Exception as exc:  # noqa: BLE001
            logger.warning("部门路由 LLM 失败(%s)，回退全部部门", exc)

        # 3) 全部部门（未命中）
        return self._result([], "all", 0.3, ["未匹配到特定部门，检索全部部门"], dept_names)

    @staticmethod
    def _keyword_match(query: str, valid: set[str]) -> tuple[list[str], list[str]]:
        matched: list[str] = []
        reasons: list[str] = []
        for dept, kws in DEPT_KEYWORDS.items():
            if dept not in valid:
                continue
            hits = [k for k in kws if k in query]
            if hits:
                matched.append(dept)
                reasons.append(f"命中关键词「{hits[0]}」")
        return matched, reasons

    @staticmethod
    def _result(dept_ids: list[str], matched_by: str, confidence: float, reasons: list[str], dept_names: dict[str, str]) -> dict[str, Any]:
        return {
            "dept_ids": dept_ids,
            "dept_names": [dept_names.get(d, d) for d in dept_ids],
            "matched_by": matched_by,
            "confidence": round(float(confidence), 2),
            "reasons": reasons,
        }
