"""Answer Agent：基于检索 chunks 生成带引用的答案，遵守 Rules 硬约束。"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.harness.base import Answer, Citation, Intent
from app.llm.client import ChatMessage, LLMClient
from app.utils.logging import get_logger
from app.integrations.pi_runtime import PiAgentRuntimeClient

logger = get_logger(__name__)

ANSWER_PROMPT = """你是芜湖市12345政务知识辅助智能体。请基于给定的官方政务文档回答受理人员的问题。

【必须遵守的规则】
{rules}

【回答要求】
- 只依据给定官方文档回答，不得编造；若材料中无明确答案，明确回答"根据当前已入库的官方政务文档未找到明确依据"。
- 每句关键结论后以 [来源1] 形式标注引用编号。
- 使用简洁、准确的中文，必要时分点说明。

【参考条款】
{chunks}

用户问题：{query}
"""


class AnswerAgent:
    def __init__(
        self, llm: LLMClient, store, pi_runtime: PiAgentRuntimeClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.llm = llm
        self.store = store
        self.pi_runtime = pi_runtime
        self.timeout = timeout

    async def generate(
        self,
        query: str,
        chunks: list[dict[str, Any]],
        rules: list[dict[str, Any]] | None = None,
        intent: Optional[Intent] = None,
        user_prefs: Optional[dict[str, Any]] = None,
        extra_instructions: str = "",
        memory_context: str = "",
    ) -> Answer:
        rules = rules or []
        rules_text = "\n".join(f"- {r.get('content', '')}" for r in rules) or "- 必须引用来源"
        if extra_instructions:
            rules_text += "\n" + extra_instructions
        if memory_context:
            rules_text += "\n以下记忆仅用于理解上下文和回答风格，不能作为制度事实或引用来源：\n" + memory_context[:2400]
        if not chunks:
            return Answer(
                content="根据现有制度文件未找到明确规定，建议咨询相关部门。",
                citations=[], dept_ids=[], generation_mode="no_evidence",
            )

        chunks_text, citations = self._format_chunks(chunks)
        generation_mode = "llm"
        try:
            prompt = ANSWER_PROMPT.format(rules=rules_text, chunks=chunks_text, query=query)
            content = None
            if self.pi_runtime is not None:
                content = await self.pi_runtime.run_text(
                    "answer", "你是芜湖市12345政务知识辅助智能体，回答严谨、有据可依。", prompt,
                    allowed_tools=[], timeout_seconds=self.timeout,
                )
            if not content:
                messages = [
                    ChatMessage.system("你是芜湖市12345政务知识辅助智能体，回答严谨、有据可依。"),
                    ChatMessage.user(prompt),
                ]
                content = await self.llm.complete(
                    messages,
                    temperature=0.2,
                    # DeepSeek v4 默认思考可能耗尽输出预算而留下空 content；
                    # 政务 RAG 已提供证据，不需要在此启用长思考。
                    thinking={"type": "disabled"},
                )
            if not isinstance(content, str) or not content.strip():
                raise ValueError("答案生成服务返回空正文")
            content = content.strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("答案生成 LLM 失败(%s)，回退原文拼接", exc)
            content = self._fallback_answer(query, chunks)
            generation_mode = "official_text_fallback"

        dept_ids = sorted({c["dept_id"] for c in chunks if c.get("dept_id")})
        return Answer(
            content=content, citations=citations, dept_ids=dept_ids,
            confidence=0.7, generation_mode=generation_mode,
        )

    def _format_chunks(self, chunks: list[dict[str, Any]]) -> tuple[str, list[Citation]]:
        lines: list[str] = []
        citations: list[Citation] = []
        for i, c in enumerate(chunks, start=1):
            lines.append(f"[来源{i}] {c.get('content', '')}")
            citations.append(
                Citation(
                    doc_id=c.get("doc_id", ""),
                    doc_title=c.get("doc_title", c.get("section_title", "")),
                    dept_id=c.get("dept_id", ""),
                    chunk_index=c.get("chunk_index", 0),
                    section_path=c.get("section_path", []),
                    snippet=c.get("content", "")[:200],
                )
            )
        return "\n\n".join(lines), citations

    @staticmethod
    def _fallback_answer(query: str, chunks: list[dict[str, Any]]) -> str:
        parts = ["根据检索到的制度条款："]
        for i, c in enumerate(chunks[:5], start=1):
            excerpt = AnswerAgent._extract_relevant_clauses(query, c.get("content", ""))
            if excerpt:
                # 一条来源对应一个连续段落，避免原文换行让后续句失去引用归属。
                excerpt = re.sub(r"\s*\n+\s*", " ", excerpt).strip()
                parts.append(f"[来源{i}] {excerpt}")
        return "\n".join(parts)

    @staticmethod
    def _extract_relevant_clauses(query: str, content: str, limit: int = 600) -> str:
        """从完整切片抽取与问题最相关的条款句，避免固定截断丢失结论。"""
        content = str(content).strip()
        if len(content) <= limit:
            return content
        normalized_query = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", query.lower())
        query_bigrams = {
            normalized_query[index:index + 2]
            for index in range(max(0, len(normalized_query) - 1))
        }
        units = [
            unit.strip(" ，,；;。")
            for unit in re.split(r"(?<=[。！？；;])|\n+", content)
            if unit.strip(" ，,；;。")
        ]
        if not units:
            return str(content)[:limit]
        policy_markers = (
            "应当", "可以", "不得", "申请", "办理", "提交", "条件", "材料", "标准",
            "时间", "期限", "责令", "处罚", "补贴", "部门", "负责", "监督", "规定",
        )
        ranked: list[tuple[float, int, str]] = []
        for index, unit in enumerate(units):
            value = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", unit.lower())
            unit_bigrams = {value[pos:pos + 2] for pos in range(max(0, len(value) - 1))}
            overlap = len(query_bigrams & unit_bigrams)
            marker_score = sum(1 for marker in policy_markers if marker in unit)
            ranked.append((overlap * 3 + marker_score, index, unit))
        # 超长异常块按相关性顺序输出，避免先恢复原顺序后再次截断时丢掉最高分条款。
        selected = sorted(ranked, reverse=True)[:3]
        excerpt = "".join(item[2] for item in selected)
        return excerpt[:limit]
