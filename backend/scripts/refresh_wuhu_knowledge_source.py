"""安全刷新一个已入库的芜湖官方页面，并同步清单和质量统计。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from scripts.crawl_wuhu_knowledge import (
    Client,
    OFFICIAL_SUFFIX,
    Record,
    extract_content,
    page_title,
    published_at,
    safe_name,
    write_manifest,
)


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"路径越界：{resolved}")
    return resolved


def _write_indexes(root: Path, rows: list[Record]) -> None:
    with (root / "source_inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["事项类别", "牵头部门", "发布部门", "文档标题", "发布日期", "正文字数", "来源URL", "本地文件"])
        for row in rows:
            writer.writerow([
                row.category, row.lead_department, row.department, row.title, row.published_at,
                row.char_count, row.source_url, row.relative_path,
            ])
    counts = Counter(row.category for row in rows)
    rejected = root / "quality_rejected.jsonl"
    quarantined = sum(1 for line in rejected.read_text(encoding="utf-8").splitlines() if line.strip()) if rejected.exists() else 0
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(rows),
        "by_category": dict(sorted(counts.items())),
        "official_only": all((urlsplit(row.source_url).hostname or "").endswith(OFFICIAL_SUFFIX) for row in rows),
        "quality_gate": {"minimum_per_category": 20, "quarantined": quarantined},
    }
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refresh(args: argparse.Namespace) -> dict:
    root = args.root.resolve()
    manifest_path = _inside(root, root / "manifest.jsonl")
    rows = [Record(**json.loads(line)) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    existing = [row for row in rows if row.source_url == args.url]
    if len(existing) != 1:
        raise ValueError(f"来源 URL 应精确匹配 1 条，实际为 {len(existing)} 条")
    previous = existing[0]
    host = urlsplit(args.url).hostname or ""
    if not host.endswith(OFFICIAL_SUFFIX):
        raise ValueError("只允许刷新芜湖政府官方域名")

    client = Client(args.delay, args.timeout)
    try:
        html = client.get_html(args.url)
    finally:
        client.close()
    if not html:
        raise RuntimeError("官方页面下载失败")
    soup = BeautifulSoup(html, "html.parser")
    title = page_title(soup)
    content = extract_content(soup, title).strip()
    if len(content) < args.minimum_chars:
        raise ValueError(f"刷新后正文仍过短：{len(content)} < {args.minimum_chars}")
    for required in args.require:
        if required not in content:
            raise ValueError(f"刷新正文缺少验收短语：{required}")

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    date = published_at(soup) or previous.published_at
    crawled = datetime.now(timezone.utc).isoformat()
    folder = _inside(root, root / previous.category / previous.department)
    folder.mkdir(parents=True, exist_ok=True)
    new_path = _inside(root, folder / f"{date or '日期未知'}_{safe_name(title)}_{digest[:10]}.md")
    header = (
        f"# {title}\n\n"
        f"- 事项类别：{previous.category}\n"
        f"- 牵头部门：{previous.lead_department}\n"
        f"- 发布部门：{previous.department}\n"
        f"- 发布日期：{date or '未识别'}\n"
        f"- 来源：{args.url}\n"
        f"- 采集时间：{crawled}\n"
        f"- 质检状态：官方页面职责正文已核验\n\n"
    )
    new_path.write_text(header + content + "\n", encoding="utf-8")

    old_path = _inside(root, root / previous.relative_path)
    if old_path != new_path and old_path.exists():
        quarantine = _inside(root, root / "_quarantine" / "replaced" / previous.relative_path)
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        if quarantine.exists():
            quarantine = quarantine.with_name(f"{quarantine.stem}_{previous.content_sha256[:8]}{quarantine.suffix}")
            _inside(root, quarantine)
        shutil.move(str(old_path), str(quarantine))

    updated = Record(
        previous.category, previous.lead_department, previous.department, title, date, args.url, crawled,
        digest, new_path.relative_to(root).as_posix(), len(content),
    )
    replaced = [updated if row.source_url == args.url else row for row in rows]
    write_manifest(manifest_path, replaced)
    _write_indexes(root, replaced)
    return {
        "source_url": args.url,
        "old_char_count": previous.char_count,
        "new_char_count": len(content),
        "new_sha256": digest,
        "new_relative_path": updated.relative_path,
        "old_file_quarantined": old_path != new_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("wuhu_knowledge_base"))
    parser.add_argument("--url", required=True)
    parser.add_argument("--minimum-chars", type=int, default=300)
    parser.add_argument("--require", action="append", default=[])
    parser.add_argument("--delay", type=float, default=0.55)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    print(json.dumps(refresh(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
