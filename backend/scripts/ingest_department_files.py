"""导入芜湖官方政务知识库。

事项类别目录 → 部门 id 映射；递归查找 pdf/docx/md/txt/html。
用法：python -m scripts.ingest_department_files [--base ../wuhu_knowledge_base]
"""
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

# 目录名（或关键词）→ dept_id 映射
DEPT_MAP = CATEGORY_TO_DEPT

SUFFIXES = {".pdf", ".docx", ".doc", ".md", ".markdown", ".txt", ".html", ".htm"}

# 候选目录（按优先级）：本地 backend/ 目录、Docker 容器内 /app、compose 挂载点
BASE_CANDIDATES = ["../wuhu_knowledge_base", "/app/wuhu_knowledge_base", "wuhu_knowledge_base"]


def resolve_base(explicit: str | None) -> Path:
    """解析示例文档根目录：优先显式指定，否则取第一个存在的候选路径。"""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p.resolve()
        print(f"警告: 指定目录不存在，尝试候选路径: {explicit}")
    for cand in BASE_CANDIDATES:
        p = Path(cand)
        if p.exists():
            return p.resolve()
    return Path(explicit or BASE_CANDIDATES[0]).resolve()


def resolve_dept(path: Path) -> str:
    for part in path.parts:
        for key, dept_id in DEPT_MAP.items():
            if key in part:
                return dept_id
    return "dept_all"


async def main(base: str, skip_conflicts: bool = False, skip_metadata_llm: bool = False) -> None:
    base_path = resolve_base(base or None)
    if not base_path.exists():
        print(f"目录不存在: {base_path}（可显式指定 --base，如 --base /app/wuhu_knowledge_base）")
        return

    settings = get_settings()
    container = build_container(settings)
    if container.mongo is not None:
        await container.mongo.connect()
    if hasattr(container.session_store, "connect"):
        try:
            await container.session_store.connect()
        except Exception:
            pass

    # 采集知识库存在清单时，只导入清单中通过质量门禁的文件。这样 `_quarantine`
    # 中可恢复的低价值材料不会被递归扫描误入 RAG。
    manifest_path = base_path / "manifest.jsonl"
    if manifest_path.exists():
        accepted_paths = {
            row["relative_path"]
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
            for row in [json.loads(line)]
        }
        files = [base_path / relative for relative in sorted(accepted_paths) if (base_path / relative).is_file()]
    else:
        files = [
            p
            for p in base_path.rglob("*")
            if p.is_file() and p.suffix.lower() in SUFFIXES and "_quarantine" not in p.parts
        ]
    print(f"发现 {len(files)} 个文档待导入")

    ok, fail = 0, 0
    for fp in files:
        dept_id = resolve_dept(fp)
        try:
            doc = await container.indexer.ingest(
                fp,
                dept_id=dept_id,
                uploaded_by="seed",
                extract_metadata=not skip_metadata_llm,
            )
            if not skip_conflicts:
                await container.conflict_detector.run_for_document(doc)
            print(f"[ok] {fp.name} -> {dept_id} ({doc['chunk_count']} chunks)")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[fail] {fp.name}: {exc}")
            fail += 1

    print(f"完成: 成功 {ok}，失败 {fail}")
    if container.mongo is not None:
        await container.mongo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="", help="芜湖政务文档根目录（默认自动探测 ../wuhu_knowledge_base 或 /app/wuhu_knowledge_base）")
    parser.add_argument(
        "--skip-conflicts",
        action="store_true",
        help="批量建库时跳过逐文档冲突检测，避免产生大量 LLM API 调用",
    )
    parser.add_argument(
        "--skip-metadata-llm",
        action="store_true",
        help="跳过 LLM 元数据抽取；用于无网络或低成本批量首次建库",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            args.base,
            skip_conflicts=args.skip_conflicts,
            skip_metadata_llm=args.skip_metadata_llm,
        )
    )
