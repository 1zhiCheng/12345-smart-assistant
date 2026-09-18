"""Query Rewriter：补全省略信息、术语标准化（查 glossary）、生成多组检索 query。"""
from __future__ import annotations
import re
from typing import Optional

from app.harness.base import Intent
from app.llm.client import ChatMessage, LLMClient
from app.storage.store import DataStore
from app.utils.logging import get_logger
from app.integrations.pi_runtime import PiAgentRuntimeClient

logger = get_logger(__name__)

REWRITE_PROMPT = """你是检索查询改写助手。将用户问题改写为 1-3 个更适合检索的 query（补全省略、规范化术语）。

术语表：
{glossary}

仅输出 JSON：
{{"queries": ["query1", "query2"]}}

用户问题：{query}
"""


class QueryRewriter:
    def __init__(
        self, llm: LLMClient, store: DataStore, pi_runtime: PiAgentRuntimeClient | None = None,
        timeout: float = 2.0,
    ) -> None:
        self.llm = llm
        self.store = store
        self.pi_runtime = pi_runtime
        self.timeout = timeout

    async def rewrite(self, query: str, intent: Optional[Intent] = None, memory_context: str = "") -> list[str]:
        glossary = await self.store.list_glossary()
        gloss_text = "; ".join(f"{g['canonical']}≈{'/'.join(g.get('synonyms', []))}" for g in glossary[:50])
        try:
            prompt = REWRITE_PROMPT.format(glossary=gloss_text, query=query) + (
                f"\n对话记忆：{memory_context[:1600]}" if memory_context else ""
            )
            data = None
            if self.pi_runtime is not None:
                data = await self.pi_runtime.run_json(
                    "rewrite", "你是检索查询改写助手。", prompt,
                    timeout_seconds=self.timeout,
                )
            if not isinstance(data, dict):
                messages = [ChatMessage.system("你是检索查询改写助手。"), ChatMessage.user(prompt)]
                data = await self.llm.complete_json(messages, temperature=0.2)
            queries = data.get("queries") if isinstance(data, dict) else None
            if isinstance(queries, list):
                model_queries = [q for q in queries if isinstance(q, str) and q.strip()]
                # 模型改写只能扩充检索，不能覆盖原问题和稳定的政务问法扩展。
                deterministic = self._glossary_expand(query, glossary)
                # 稳定扩展排在模型改写之前，且只吸收一条模型 query，避免多条宽泛
                # 改写在多查询融合时稀释精准法规条款。
                return self._dedupe([query, *deterministic, *model_queries[:1]])[:3]
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询改写失败(%s)，使用原 query + 术语扩展", exc)
        return self._glossary_expand(query, glossary)

    def _glossary_expand(self, query: str, glossary: list[dict]) -> list[str]:
        """术语表与通用政务问法扩展（无 LLM 回退）。"""
        queries = [query]
        # 复合诉求分开召回，避免一个主题的高频切片挤掉另一个主题的直接依据。
        clauses = [
            part.strip(" ，,。？?；;")
            for part in re.split(r"[，,；;]|另外|同时咨询|以及", query)
            if len(part.strip(" ，,。？?；;")) >= 6
        ]
        if len(clauses) > 1:
            queries.extend(clauses[:3])
        expanded = query
        for g in glossary:
            canonical = g.get("canonical", "")
            if canonical and canonical in query:
                for syn in g.get("synonyms", []):
                    if syn and syn not in expanded:
                        expanded += f" {syn}"
        if expanded != query:
            queries.append(expanded)
        question_expansions = (
            (("怎么办", "如何办理", "如何申请", "申请条件"), "办理流程 申请条件 申请材料"),
            (("哪些材料", "什么材料", "所需材料"), "申请材料 所需材料 提交材料"),
            (("如何处理", "怎么处理", "不按规定", "违规"), "处理规定 责令改正 处罚"),
            (("有什么规定", "什么规定"), "管理规定 适用条件 办理证件"),
            (("政策依据", "调整依据"), "政策依据 执行时间 调整标准"),
            (("如何招生", "招生入学"), "招生原则 招生对象 招生办法 入学条件"),
            (("常见问题", "经办问题"), "施行时间 适用范围 经办原则"),
            (("哪些职责", "什么职责", "履行职责"), "应当履行 职责 监督 管理"),
            (("向哪里反映", "哪里投诉", "如何投诉"), "主管部门 投诉举报 处理措施"),
            (("相关政策", "补贴政策"), "政策标准 申请条件 补贴标准"),
        )
        for markers, suffix in question_expansions:
            if any(marker in query for marker in markers):
                queries.append(f"{query} {suffix}")
        return self._dedupe(queries)

    @staticmethod
    def _dedupe(queries: list[str]) -> list[str]:
        seen: list[str] = []
        for q in queries:
            if q and q not in seen:
                seen.append(q)
        return seen or ["*"]
