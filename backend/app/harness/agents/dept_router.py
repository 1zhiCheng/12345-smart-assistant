"""Dept Router（自动部门路由 Agent）：把 12345 群众诉求匹配到承办部门。

策略：关键词精确匹配（快速、可解释）→ LLM 语义路由（兜底）→ 全部部门。
返回路由结果（dept_ids / matched_by / confidence / reasons / dept_names），
供前端展示"已自动路由到 XX 部门"。
"""
from __future__ import annotations

from typing import Any

from app.llm.client import ChatMessage, LLMClient
from app.llm.embeddings import EmbeddingClient
from app.storage.store import DataStore
from app.utils.logging import get_logger
from app.domain.wuhu import DEPARTMENT_KEYWORDS
from app.harness.agents.semantic_dept_router import SemanticDepartmentRouter

logger = get_logger(__name__)

# 部门关键词路由表（覆盖全部部门；命中即路由到对应部门，可多部门）
DEPT_KEYWORDS = DEPARTMENT_KEYWORDS

# 高区分度复合事项先于单个通用词判断，避免“施工噪声”同时被“施工”和“噪声”
# 命中后因部门表顺序误排到住建。这里只覆盖边界明确的比赛事项。
PRIORITY_PHRASES: dict[str, tuple[str, ...]] = {
    "dept_market_regulation": (
        "食品安全", "商品质量", "消费维权", "价格欺诈", "未明码标价", "明码标价", "价目表",
        "消费纠纷", "报修三次", "退换货", "营业执照", "新能源充电桩",
    ),
    "dept_labor_social_security": ("拖欠工资", "欠薪", "劳动仲裁"),
    "dept_city_management": ("占道经营", "盲道占用", "渣土乱倒", "路灯不亮"),
    "dept_housing_construction": ("房屋漏水", "车库漏水", "路面沉降", "排水设施", "房屋拆迁", "拆迁选房", "安置房源"),
    "dept_public_security": ("消防通道", "消防栓撞坏", "信号灯故障", "冒充公安", "诈骗电话"),
    "dept_public_services": ("罐装液化气", "送气服务", "突然停水", "断电", "频繁停电", "燃气充值", "燃气费", "供电故障"),
    "dept_transportation": ("公交线路", "公交不进站", "出租车拒载", "农村公路", "公路护栏"),
    "dept_economy_trade": ("抵押贷款", "贷款合同", "违约利息", "技改补贴", "技术改造补贴", "智能化改造", "电子发票", "涉企收费", "企业办贷款"),
    "dept_education_science_culture_sports": ("幼升小报名", "舞蹈培训", "未消费部分学费", "兴趣课", "培训机构退费", "图书馆开放", "体育馆"),
    "dept_agriculture_forestry_water": ("申请宅基地", "农田灌溉", "水渠", "宅基地审批", "塘坝", "水库水位", "水利", "防汛"),
    "dept_ecology_environment": ("施工噪声", "夜间施工噪声", "工地噪声", "灰黑", "偷排", "渣土车", "土堆"),
    "dept_health": ("外科就诊", "无菌要求", "医院收费", "材料费", "疫苗预约", "预防接种", "医美注射", "医疗机构执业许可"),
}

ROUTE_PROMPT = """你是芜湖 12345 事项分类与部门路由助手。判断群众诉求应由哪个部门承办，输出 JSON：
{{"depts": ["dept_id"], "confidence": 0.0-1.0, "reason": "简短理由"}}

候选部门：
{departments}

群众诉求：{query}
"""


class DeptRouter:
    """自动部门路由：群众诉求 → 最匹配的承办部门。"""

    def __init__(self, llm: LLMClient, store: DataStore, embeddings: EmbeddingClient | None = None) -> None:
        self.llm = llm
        self.store = store
        settings = getattr(embeddings, "settings", None)
        self.semantic = (
            SemanticDepartmentRouter(embeddings, getattr(settings, "routing_prototype_path", ""))
            if embeddings is not None and getattr(settings, "routing_semantic_enabled", True)
            else None
        )

    async def route(self, query: str) -> dict[str, Any]:
        departments = await self.store.list_departments()
        dept_names = {d["_id"]: d.get("name", d["_id"]) for d in departments}
        valid = set(dept_names)

        # 1) 高区分度复合事项继续直接命中，保证明确场景稳定且可解释。
        priority, priority_reasons = self._priority_match(query, valid)
        if priority:
            return self._result(priority, "keyword", 0.95, priority_reasons, dept_names)

        # 2) 通用关键词与官方文档语义原型融合。原型只由冻结集之外的文档生成。
        matched, reasons = self._keyword_match(query, valid)
        semantic_scores = await self.semantic.rank(query, valid) if self.semantic is not None else []
        if semantic_scores:
            keyword_bonus = float(getattr(self.semantic.embeddings.settings, "routing_keyword_bonus", 0.08))
            score_by_dept = dict(semantic_scores)
            for dept_id in matched:
                score_by_dept[dept_id] = score_by_dept.get(dept_id, 0.0) + keyword_bonus
            ranked = sorted(score_by_dept, key=lambda dept_id: (-score_by_dept[dept_id], dept_id))
            top_k = max(1, int(getattr(self.semantic.embeddings.settings, "routing_semantic_top_k", 3)))
            selected = ranked[:top_k]
            top_score = score_by_dept[selected[0]]
            semantic_reasons = [f"官方文档语义原型相似度 {top_score:.3f}"]
            if reasons:
                semantic_reasons.append(reasons[0])
            return self._result(
                selected, "semantic_hybrid", min(0.95, max(0.5, top_score)), semantic_reasons, dept_names
            )

        # 语义模型/原型不可用时保持原关键词行为。
        if matched:
            return self._result(matched, "keyword", min(0.95, 0.6 + 0.1 * len(matched)), reasons, dept_names)

        # 3) LLM 语义路由（本地语义与关键词均不可用时）
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

        # 4) 全部部门（未命中）
        return self._result([], "all", 0.3, ["未匹配到特定部门，检索全部部门"], dept_names)

    @staticmethod
    def _priority_match(query: str, valid: set[str]) -> tuple[list[str], list[str]]:
        for dept, phrases in PRIORITY_PHRASES.items():
            if dept not in valid:
                continue
            hit = next((phrase for phrase in phrases if phrase in query), None)
            if hit:
                return [dept], [f"命中高区分度事项「{hit}」"]
        return [], []

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
