"""测试 Loop 层（规则/钩子引擎 + Skill Miner 聚类）。"""
from __future__ import annotations

import pytest

from app.harness.base import Intent
from app.loop.hook_engine import HookEngine
from app.loop.rule_engine import RuleEngine
from app.loop.skill_miner import SkillMiner
from app.storage.store import MemoryStore


@pytest.mark.asyncio
async def test_rule_engine_seed_and_active():
    store = MemoryStore()
    engine = RuleEngine(store)
    await engine.seed_defaults()
    rules = await engine.active_rules()
    assert any(r["name"] == "no_guess_rule" for r in rules)


@pytest.mark.asyncio
async def test_hook_engine_apply():
    store = MemoryStore()
    engine = HookEngine(store)
    await engine.seed_defaults()
    hooks = await engine.active_hooks()
    intent = Intent(type="complaint", depts=["dept_city_management"], raw={"query": "小区施工噪声扰民"})
    depts = await engine.apply(hooks, intent, ["dept_city_management"])
    assert "dept_housing_construction" in depts
    assert "dept_ecology_environment" in depts


def test_skill_miner_cluster():
    miner = SkillMiner(MemoryStore(), llm=None, min_cluster=2)
    queries = ["垃圾分类时间是什么", "垃圾分类什么时候开始", "住房补贴怎么办理", "住房补贴流程是什么"]
    # 用 hash 向量近似（维度一致即可）
    vecs = [[0.1, 0.2], [0.1, 0.25], [0.9, 0.8], [0.9, 0.85]]
    clusters = miner.cluster(queries, vecs)
    assert clusters  # 至少有一个簇


def test_skill_miner_keyword_fallback():
    miner = SkillMiner(MemoryStore(), llm=None, min_cluster=2)
    queries = ["垃圾分类时间是什么", "垃圾分类什么时候开始", "垃圾分类截止日期"]
    clusters = miner._keyword_group(queries)
    assert clusters
