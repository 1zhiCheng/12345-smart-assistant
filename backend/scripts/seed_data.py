"""种子数据：芜湖 12345 部门 / 术语表 / 默认 Rules&Hooks。

用法：python -m scripts.seed_data
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.config import get_settings
from app.deps import build_container
from app.loop.default_skills import seed_default_skills
from app.domain.wuhu import DEPARTMENTS, seed_wuhu_departments

GLOSSARY = [
    {"canonical": "12345政务服务便民热线", "synonyms": ["12345", "市长热线", "政务热线", "便民热线"]},
    {"canonical": "营业员", "synonyms": ["接线员", "话务员", "热线受理员"]},
    {"canonical": "群众诉求", "synonyms": ["来电诉求", "市民反映", "投诉事项", "咨询事项"]},
    {"canonical": "转派", "synonyms": ["派单", "流转", "交办", "承办部门推荐"]},
    {"canonical": "政策依据", "synonyms": ["法律依据", "文件依据", "条款依据", "办事指南"]},
]


async def main() -> None:
    settings = get_settings()
    container = build_container(settings)
    if container.mongo is not None:
        await container.mongo.connect()
    if hasattr(container.session_store, "connect"):
        try:
            await container.session_store.connect()
        except Exception:
            pass

    now = datetime.now(timezone.utc).isoformat()
    store = container.store

    created = await seed_wuhu_departments(store)
    for dept in DEPARTMENTS:
        print(f"[dept] {dept['_id']} {dept['name']}")
    print(f"[dept] 新增 {created} 个芜湖政务部门")

    for i, g in enumerate(GLOSSARY):
        entry = {
            "_id": f"glossary_seed_{i}",
            "canonical": g["canonical"],
            "synonyms": g["synonyms"],
            "dept_id": "",
            "created_by": "seed",
            "created_at": now,
        }
        await store.upsert_glossary(entry)
    print(f"[glossary] {len(GLOSSARY)} 条")

    await container.rule_engine.seed_defaults()
    await container.hook_engine.seed_defaults()
    print("[rules/hooks] 默认规则与钩子已种子化")
    created_skills = await seed_default_skills(store)
    print(f"[skills] 可执行基线 Skill 已就绪（本次新增 {created_skills}）")

    if container.mongo is not None:
        await container.mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
