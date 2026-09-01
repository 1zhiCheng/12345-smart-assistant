"""将旧高校行政数据迁移为芜湖 12345 比赛场景。

默认仅预览；使用 --apply 才会写入。写入前会把将删除的记录完整备份为 JSON。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.deps import build_container
from app.domain.wuhu import LEGACY_DEPARTMENT_IDS, seed_wuhu_departments
from app.loop.default_skills import seed_default_skills


LEGACY_USERS = {"student", "jwc_admin", "cwc_admin"}
LEGACY_SKILLS = {
    "skill_dept_hqaq_emergency_seed", "skill_dept_zfxy_procedure_seed", "skill_academic_deadline_seed",
}
LEGACY_HOOKS = {"hook_cross_dept"}
LEGACY_GLOSSARY_CANONICALS = {"辅导员", "退课", "学费", "选课", "学分"}


def contains_legacy_dept(row: dict[str, Any]) -> bool:
    values = [row.get("dept_id"), *(row.get("dept_ids") or [])]
    return any(value in LEGACY_DEPARTMENT_IDS for value in values)


async def migrate(apply: bool, backup_dir: Path) -> dict[str, int]:
    settings = get_settings()
    if settings.storage_mode != "mongo":
        raise RuntimeError("迁移脚本必须在 STORAGE_MODE=mongo 下运行，避免误以为已清理持久化数据")
    container = build_container(settings)
    await container.mongo.connect()
    store = container.store
    try:
        legacy_docs = [row for row in await store.find("documents") if contains_legacy_dept(row)]
        legacy_doc_ids = {row["_id"] for row in legacy_docs}
        targets: dict[str, list[dict[str, Any]]] = {
            "departments": [row for row in await store.find("departments") if row.get("_id") in LEGACY_DEPARTMENT_IDS],
            "documents": legacy_docs,
            "chunks": [row for row in await store.find("chunks") if row.get("doc_id") in legacy_doc_ids or contains_legacy_dept(row)],
            "vector_embeddings": [row for row in await store.find("vector_embeddings") if row.get("doc_id") in legacy_doc_ids or contains_legacy_dept(row)],
            "doc_relations": [row for row in await store.find("doc_relations") if row.get("source_doc_id") in legacy_doc_ids or row.get("target_doc_id") in legacy_doc_ids or contains_legacy_dept(row)],
            "skills": [row for row in await store.find("skills") if row.get("_id") in LEGACY_SKILLS or contains_legacy_dept(row)],
            "hooks": [row for row in await store.find("hooks") if row.get("_id") in LEGACY_HOOKS or contains_legacy_dept(row)],
            "users": [row for row in await store.find("users") if row.get("_id") in LEGACY_USERS or row.get("role") in {"student", "teacher"} or contains_legacy_dept(row)],
            "glossary": [
                row for row in await store.find("glossary")
                if row.get("canonical") in LEGACY_GLOSSARY_CANONICALS
            ],
            "dept_memory": [row for row in await store.find("dept_memory") if contains_legacy_dept(row) or row.get("_id") in LEGACY_DEPARTMENT_IDS],
            "review_orders": [row for row in await store.find("review_orders") if contains_legacy_dept(row) or row.get("doc_id") in legacy_doc_ids],
            "test_questions": [row for row in await store.find("test_questions") if contains_legacy_dept(row)],
            "org_memory_items": [row for row in await store.find("org_memory_items") if contains_legacy_dept(row) or any(ref.get("doc_id") in legacy_doc_ids for ref in row.get("source_refs", [])) or row.get("_id") == "orgmem_global_calendar"],
            "faq_cache": [row for row in await store.find("faq_cache") if contains_legacy_dept(row)],
            "memory_topics": [row for row in await store.find("memory_topics") if contains_legacy_dept(row)],
        }
        strategy_ids = {row["_id"] for row in targets["skills"]}
        targets["strategy_versions"] = [
            row for row in await store.find("strategy_versions")
            if row.get("artifact_id") in strategy_ids or contains_legacy_dept(row)
        ]
        targets["strategy_executions"] = [row for row in await store.find("strategy_executions") if contains_legacy_dept(row) or row.get("skill_id") in strategy_ids]
        counts = {name: len(rows) for name, rows in targets.items() if rows}
        if not apply:
            return counts

        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"legacy_wenshu_backup_{stamp}.json"
        backup_path.write_text(json.dumps(targets, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        for collection, rows in targets.items():
            for row in rows:
                await store.delete(collection, row["_id"])

        # admin 用户名保留，但将旧 role 原地迁移为系统管理员。
        admin = await store.get("users", "admin")
        if admin:
            admin["role"] = "system_admin"
            admin["dept_id"] = ""
            admin["name"] = "系统管理员"
            await store.upsert_user(admin)

        await seed_wuhu_departments(store)
        await container.rule_engine.seed_defaults()
        await container.hook_engine.seed_defaults()
        await seed_default_skills(store)
        await container.auth.seed_users()
        counts["backup_file"] = str(backup_path)  # type: ignore[assignment]
        return counts
    finally:
        await container.mongo.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path, default=Path("../backups"))
    args = parser.parse_args()
    result = asyncio.run(migrate(args.apply, args.backup_dir.resolve()))
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "targets": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
