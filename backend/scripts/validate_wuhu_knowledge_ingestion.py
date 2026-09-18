"""验收官方知识库清单，并用生产解析/切片链路完成一次内存 RAG 入库。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from app.config import get_settings
from app.deps import build_container
from app.domain.wuhu import CATEGORY_TO_DEPT, seed_wuhu_departments


METADATA_LINE = re.compile(r"^-\s*(事项类别|牵头部门|发布部门|发布日期|来源|采集时间|质检状态)[：:]")
REQUIRED_12315 = ("拟订保护消费者权益", "指导开展市场监管咨询", "承担网络交易纠纷")


def _content(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    last_metadata = max((index for index, line in enumerate(lines) if METADATA_LINE.match(line)), default=0)
    return "\n".join(lines[last_metadata + 1:]).strip()


async def validate(args: argparse.Namespace) -> dict:
    root = args.root.resolve()
    rows = [
        json.loads(line) for line in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    errors: list[str] = []
    counts = Counter(row["category"] for row in rows)
    if any(counts[category] < args.minimum_per_category for category in CATEGORY_TO_DEPT):
        errors.append("有类别未达到最小官方文档数")
    if len({row["source_url"] for row in rows}) != len(rows):
        errors.append("清单存在重复 URL")
    if len({row["content_sha256"] for row in rows}) != len(rows):
        errors.append("清单存在重复正文哈希")

    content_by_url: dict[str, str] = {}
    for row in rows:
        path = (root / row["relative_path"]).resolve()
        if root not in path.parents or not path.is_file():
            errors.append(f"文件缺失或越界:{hashlib.sha256(row['relative_path'].encode()).hexdigest()[:12]}")
            continue
        host = urlsplit(row["source_url"]).hostname or ""
        if not host.endswith(".wuhu.gov.cn"):
            errors.append(f"非芜湖政府官方来源:{hashlib.sha256(row['source_url'].encode()).hexdigest()[:12]}")
        content = _content(path)
        content_by_url[row["source_url"]] = content
        if len(content) < args.minimum_chars:
            errors.append(f"正文过短:{hashlib.sha256(row['relative_path'].encode()).hexdigest()[:12]}")
        if len(content) != int(row["char_count"]):
            errors.append(f"正文字数不一致:{hashlib.sha256(row['relative_path'].encode()).hexdigest()[:12]}")
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != row["content_sha256"]:
            errors.append(f"正文哈希不一致:{hashlib.sha256(row['relative_path'].encode()).hexdigest()[:12]}")

    duties = [
        content for url, content in content_by_url.items()
        if url == "https://amr.wuhu.gov.cn/openness/public/6596721/20949411.html"
    ]
    if len(duties) != 1 or any(phrase not in duties[0] for phrase in REQUIRED_12315):
        errors.append("12315 职责正文不完整")

    settings = get_settings().model_copy(update={
        "storage_mode": "memory", "vector_backend": "memory", "embedding_provider": "hash",
        "deepseek_api_key": "", "pi_agent_enabled": False, "routing_semantic_enabled": False,
    })
    container = build_container(settings)
    await seed_wuhu_departments(container.store)
    indexed = 0
    for row in rows:
        path = root / row["relative_path"]
        if not path.is_file():
            continue
        try:
            await container.indexer.ingest(
                path, dept_id=CATEGORY_TO_DEPT[row["category"]], uploaded_by="knowledge-gate",
                extract_metadata=False,
            )
            indexed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"入库失败:{hashlib.sha256(row['relative_path'].encode()).hexdigest()[:12]}:{type(exc).__name__}")

    documents = await container.store.list_documents(status="active")
    chunks = await container.store.list_active_chunks()
    vector_count = await container.vector_store.count()
    chunk_counts = Counter(chunk.get("metadata", {}).get("category", "") for chunk in chunks)
    if indexed != len(rows) or len(documents) != len(rows):
        errors.append("活动文档数与清单不一致")
    if len(chunks) != vector_count:
        errors.append("chunk 与向量数不一致")
    if any(not chunk.get("retrieval_text") for chunk in chunks):
        errors.append("存在缺少 retrieval_text 的 chunk")
    if any(not chunk.get("metadata", {}).get("source_url") for chunk in chunks):
        errors.append("存在缺少官方来源 URL 的 chunk")
    if any(chunk.get("char_count", 0) > 600 for chunk in chunks):
        errors.append("存在超过 600 字的 chunk")

    report = {
        "status": "passed" if not errors else "failed",
        "manifest_documents": len(rows),
        "indexed_documents": indexed,
        "chunks": len(chunks),
        "vectors": vector_count,
        "minimum_per_category": args.minimum_per_category,
        "minimum_chars": args.minimum_chars,
        "official_only": all((urlsplit(row["source_url"]).hostname or "").endswith(".wuhu.gov.cn") for row in rows),
        "unique_urls": len({row["source_url"] for row in rows}) == len(rows),
        "unique_content_hashes": len({row["content_sha256"] for row in rows}) == len(rows),
        "content_hashes_verified": not any("哈希" in error for error in errors),
        "required_12315_duties_verified": not any("12315" in error for error in errors),
        "documents_by_category": dict(sorted(counts.items())),
        "chunks_by_category": dict(sorted(chunk_counts.items())),
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if errors:
        raise SystemExit(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("wuhu_knowledge_base"))
    parser.add_argument("--output", type=Path, default=Path("docs/competition/wuhu_knowledge_ingestion_gate.json"))
    parser.add_argument("--minimum-per-category", type=int, default=20)
    parser.add_argument("--minimum-chars", type=int, default=180)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(validate(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
