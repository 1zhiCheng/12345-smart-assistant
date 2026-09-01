"""Intent Agent：意图识别 / 部门路由 / 用户身份 / 是否需要跨部门。"""
from __future__ import annotations

from typing import Any

from app.harness.base import Intent
from app.llm.client import ChatMessage, LLMClient
from app.storage.store import DataStore
from app.utils.logging import get_logger
from app.integrations.pi_runtime import PiAgentRuntimeClient
from app.domain.wuhu import DEPARTMENT_KEYWORDS

logger = get_logger(__name__)

INTENT_PROMPT = """你是芜湖 12345 政务热线意图识别助手。判断群众诉求的事项类型、承办部门和是否需要跨部门协同。

仅输出 JSON：
{{
  "type": "regulation_consult|process_guide|deadline_query|complaint|chitchat|other",
  "depts": ["dept_id"],           // 涉及的部门 id，从候选部门中选择；无法确定则 ["dept_all"]
  "user_role": "operator|department_admin|system_admin",
  "entities": {{}},               // 关键实体，如 {{"matter": "施工噪声", "location": "镜湖区"}}
  "needs_cross_dept": false,      // 是否涉及多个部门
  "confidence": 0.0-1.0
}}

候选部门列表：
{departments}

用户画像：{profile}

用户问题：{query}
"""


class IntentAgent:
    def __init__(
        self, llm: LLMClient, store: DataStore, pi_runtime: PiAgentRuntimeClient | None = None,
        timeout: float = 1.0,
    ) -> None:
        self.llm = llm
        self.store = store
        self.pi_runtime = pi_runtime
        self.timeout = timeout

    async def infer(self, query: str, user_id: str = "", memory_context: str = "") -> Intent:
        departments = await self.store.list_departments()
        dept_desc = ", ".join(f"{d['_id']}({d.get('name', '')})" for d in departments) or "dept_all(通用)"
        profile = await self.store.get_user_profile(user_id) or {}
        profile_text = memory_context or str(profile)[:500]

        try:
            prompt = INTENT_PROMPT.format(departments=dept_desc, profile=profile_text[:2000], query=query)
            data = None
            if self.pi_runtime is not None:
                data = await self.pi_runtime.run_json(
                    "intent", "你是严谨的制度咨询意图识别助手。", prompt,
                    timeout_seconds=self.timeout,
                )
            if not isinstance(data, dict):
                messages = [
                    ChatMessage.system("你是严谨的意图识别助手。"),
                    ChatMessage.user(prompt),
                ]
                data = await self.llm.complete_json(messages, temperature=0.0)
            intent = Intent.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("意图识别 LLM 失败(%s)，使用关键词回退", exc)
            intent = self._keyword_fallback(query)

        # 规范化：确保 dept 有效
        valid_depts = {d["_id"] for d in departments}
        if not intent.depts or (len(intent.depts) == 1 and intent.depts[0] == "dept_all"):
            intent.depts = sorted(valid_depts) if valid_depts else ["dept_all"]
        intent.depts = [d for d in intent.depts if d in valid_depts or d == "dept_all"] or ["dept_all"]
        return intent

    @staticmethod
    def _keyword_fallback(query: str) -> Intent:
        """无 LLM 时的关键词规则回退。"""
        dept_keywords = DEPARTMENT_KEYWORDS
        depts = [d for d, kws in dept_keywords.items() if any(k in query for k in kws)]
        intent_type = "other"
        if any(k in query for k in ["截止", "什么时候", "最晚", "deadline", "时间"]):
            intent_type = "deadline_query"
        elif any(k in query for k in ["怎么办", "如何", "流程", "怎么办理", "步骤"]):
            intent_type = "process_guide"
        elif any(k in query for k in ["投诉", "建议", "举报"]):
            intent_type = "complaint"
        elif any(k in query for k in ["你好", "谢谢", "在吗"]):
            intent_type = "chitchat"
        elif depts:
            intent_type = "regulation_consult"
        return Intent(
            type=intent_type,
            depts=depts or ["dept_all"],
            needs_cross_dept=len(depts) > 1,
            confidence=0.6,
            raw={"fallback": True},
        )
