"""按人工确认的来源白名单清理芜湖政务知识库。

仅保留白名单 URL，同步类别与部门，修复站点通用标题，并生成可用 Excel
打开的来源清单和质量统计。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


CATEGORY_ORDER = [
    "城市管理", "城乡建设", "公共安全", "公共服务", "交通运输", "经济财贸",
    "科教文体", "劳动和社会保障", "农林水土", "生态环境", "市场监管", "卫生健康",
]


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(".")
    return value[:100] or "未命名文档"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--curated-file", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    manifest = root / "manifest.jsonl"
    curated = json.loads(args.curated_file.resolve().read_text(encoding="utf-8"))
    allowed = {item["url"]: item for item in curated}
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept: list[dict] = []
    removed = 0

    for row in rows:
        source = allowed.get(row["source_url"])
        old_path = (root / row["relative_path"]).resolve()
        if root not in old_path.parents:
            raise RuntimeError(f"文件越界: {old_path}")
        if source is None:
            if old_path.exists():
                old_path.unlink()
            removed += 1
            continue

        row["category"] = source["category"]
        row["lead_department"] = source["lead_department"]
        row["department"] = source["department"]
        title = source.get("title", "").strip() or row["title"]
        if title != row["title"] and old_path.exists():
            body = old_path.read_text(encoding="utf-8")
            body = re.sub(r"^# .*?$", f"# {title}", body, count=1, flags=re.MULTILINE)
            new_folder = root / row["category"] / row["department"]
            new_folder.mkdir(parents=True, exist_ok=True)
            new_name = f"{row['published_at'] or '日期未知'}_{safe_name(title)}_{row['content_sha256'][:10]}.md"
            new_path = new_folder / new_name
            new_path.write_text(body, encoding="utf-8")
            if new_path.resolve() != old_path and old_path.exists():
                old_path.unlink()
            row["relative_path"] = new_path.relative_to(root).as_posix()
            row["title"] = title
        kept.append(row)

    order = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    kept.sort(key=lambda row: (order.get(row["category"], 99), row["department"], row["published_at"], row["title"]))
    manifest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept), encoding="utf-8")

    with (root / "source_inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["事项类别", "牵头部门", "发布部门", "文档标题", "发布日期", "正文字数", "来源URL", "本地文件"])
        for row in kept:
            writer.writerow([row["category"], row["lead_department"], row["department"], row["title"], row["published_at"], row["char_count"], row["source_url"], row["relative_path"]])

    counts = Counter(row["category"] for row in kept)
    short = [row for row in kept if row.get("char_count", 0) < 350]
    report = {
        "total": len(kept),
        "removed_not_in_curated_whitelist": removed,
        "official_only": True,
        "by_category": {category: counts[category] for category in CATEGORY_ORDER},
        "short_documents_under_350_chars": [
            {"category": row["category"], "title": row["title"], "char_count": row["char_count"], "url": row["source_url"]}
            for row in short
        ],
    }
    (root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
