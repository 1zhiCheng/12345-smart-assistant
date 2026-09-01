"""Built-in baseline Skills used by the real runtime and the admin demo.

These are idempotent, executable workflow policies rather than display-only rows.
They provide a safe baseline before enough production traces exist for Skill Miner.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.storage.store import DataStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_SKILLS: list[dict[str, Any]] = [
    {
        "_id": "skill_work_order_intake_quality_seed",
        "name": "12345工单要素完整性核验",
        "description": "对群众诉求中的时间、地点、对象、事件经过和诉求进行结构化核验。",
        "dept_id": "", "scope": "global",
        "trigger": {
            "intent_patterns": ["反映", "投诉", "求助", "咨询", "举报"],
            "entities_required": ["matter"], "confidence_threshold": 0.75,
        },
        "action": {
            "type": "workflow",
            "steps": [
                {"step": 1, "action": "extract_entity", "params": {"entity": "matter"}},
                {"step": 2, "action": "retrieve", "params": {"query": "{matter} 受理条件 办理部门 政策依据", "top_k": 8}},
                {"step": 3, "action": "generate", "params": {"template": "事实要素-群众诉求-待补充信息"}},
            ],
        },
        "unique_rules": ["不得把接线员的追问或复述当作群众事实；原文未提及的事实不得补写。"],
        "rubric_rules": ["标准工单必须区分事实、诉求、缺失字段和澄清问题。"],
    },
    {
        "_id": "skill_policy_grounded_reply_seed",
        "name": "政策依据与回复生成",
        "description": "使用芜湖官方政务文档为转派和回复提供可追溯依据。",
        "dept_id": "", "scope": "global",
        "trigger": {
            "intent_patterns": ["政策", "依据", "怎么办理", "该哪个部门", "如何回复"],
            "entities_required": ["matter"], "confidence_threshold": 0.72,
        },
        "action": {
            "type": "workflow",
            "steps": [
                {"step": 1, "action": "extract_entity", "params": {"entity": "matter"}},
                {"step": 2, "action": "retrieve", "params": {"query": "{matter} 条例 办法 办事指南 部门职责", "top_k": 8}},
                {"step": 3, "action": "generate", "params": {"template": "受理意见-政策依据-处理建议-回复口径"}},
            ],
        },
        "unique_rules": ["政策结论只能来自检索到的官方文档；关键结论必须附标题与原文片段。"],
        "rubric_rules": ["无明确依据时标记待部门人工确认，不得使用常识补全政策。"],
    },
    {
        "_id": "skill_emergency_dispatch_seed",
        "name": "紧急事项风险提示",
        "description": "识别涉及人身安全、重大污染、燃气泄漏等紧急诉求，提示营业员优先人工升级。",
        "dept_id": "dept_public_security", "scope": "department",
        "trigger": {
            "intent_patterns": ["燃气泄漏", "火灾", "爆炸", "人身安全", "危险品", "重大污染"],
            "entities_required": ["matter"], "confidence_threshold": 0.78,
        },
        "action": {
            "type": "workflow",
            "steps": [
                {"step": 1, "action": "retrieve", "params": {"query": "{matter} 应急处置 主管部门 安全提示", "top_k": 8}},
                {"step": 2, "action": "generate", "params": {"template": "危险提示-人工升级-转派建议"}},
            ],
        },
        "unique_rules": ["系统只做辅助判断，不替代 110、119、120 等紧急渠道；营业员必须人工确认。"],
        "rubric_rules": ["紧急风险提示应置于政策回复之前。"],
    },
]


async def seed_default_skills(store: DataStore) -> int:
    created = 0
    for template in DEFAULT_SKILLS:
        if await store.get_skill(template["_id"]) is not None:
            continue
        skill = {
            **template, "metrics": {
                "trigger_count": 0, "success_count": 0, "success_rate": 0.0,
                "avg_latency_ms": 0, "last_triggered": "",
            },
            "version": 1, "status": "active", "auto_generated": False,
            "confidence": 1.0, "gray_percent": 1.0, "created_by": "system_seed",
            "origin": "builtin_baseline", "created_at": _now(),
        }
        await store.upsert_skill(skill)
        await store.upsert("strategy_versions", {
            "_id": f"strategy_version_{skill['_id']}_v1_seed",
            "artifact_id": skill["_id"], "artifact_type": "skill", "version": 1,
            "reason": "builtin_baseline_seeded", "snapshot": skill, "created_at": _now(),
        })
        created += 1
    return created
