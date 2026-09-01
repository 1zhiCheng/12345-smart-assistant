"""将文件质量门禁隔离的文档同步归档到 MongoDB，并移除其向量索引。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.deps import build_container
from app.domain.wuhu import CATEGORY_TO_DEPT

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main(rejected_path: Path) -> None:
    rejected = [
        json.loads(line)
        for line in rejected_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # 同标题重复项在 MongoDB 中本就只保留一份；不能按标题归档，否则会误伤
    # 通过门禁的那份有效文档。
    targets = {
        (CATEGORY_TO_DEPT[row["category"]], row["title"])
        for row in rejected
        if row.get("quality_reject_reason") != "同类别重复标题"
    }
    container = build_container(get_settings())
    if container.mongo is not None:
        await container.mongo.connect()
    archived = 0
    for doc in await container.store.list_documents(status="active"):
        if (doc.get("dept_id"), doc.get("title")) in targets:
            await container.indexer.set_status(doc["_id"], "archived")
            archived += 1
            print(f"[归档] {doc.get('dept_id')}: {doc.get('title')}")
    print(json.dumps({"quality_rejections": len(targets), "archived_active_documents": archived}, ensure_ascii=False))
    if container.mongo is not None:
        await container.mongo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rejected", type=Path, default=Path("../wuhu_knowledge_base/quality_rejected.jsonl"))
    args = parser.parse_args()
    asyncio.run(main(args.rejected.resolve()))
