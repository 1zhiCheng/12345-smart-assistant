"""从官方知识库确定性抽取并冻结 12 类部门路由留出集。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app.domain.wuhu import CATEGORY_TO_DEPT


def clean_markdown(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8-sig", errors="ignore")
    title_match = re.search(r"(?m)^#\s+(.+)$", raw)
    title = title_match.group(1).strip() if title_match else path.stem
    lines = []
    for line in raw.splitlines():
        value = line.strip()
        if not value or value.startswith(("#", "- 事项类别：", "- 牵头部门：", "- 发布部门：", "- 发布日期：", "- 来源：", "- 采集时间：", "- 质检状态：")):
            continue
        lines.append(value)
    return title, "\n".join(lines)[:1600]


def build(corpus: Path, per_category: int = 2) -> dict:
    cases = []
    for category, dept_id in CATEGORY_TO_DEPT.items():
        category_root = corpus / category
        candidates = []
        paths = category_root.rglob("*.md") if category_root.exists() else []
        for path in paths:
            title, body = clean_markdown(path)
            if len(body) < 120:
                continue
            key = hashlib.sha256(f"wuhu-routing-holdout-v1:{path.relative_to(corpus)}".encode("utf-8")).hexdigest()
            candidates.append((key, path, title, body))
        for index, (_, path, title, body) in enumerate(sorted(candidates)[:per_category], start=1):
            cases.append({
                "id": f"holdout-{dept_id.removeprefix('dept_')}-{index:02d}",
                "category": category, "department_id": dept_id,
                "source_file": str(path.relative_to(corpus)),
                "text": f"{title}\n{body}",
            })
    return {
        "metadata": {
            "frozen_at": datetime.now(timezone.utc).isoformat(),
            "source": str(corpus), "sampling": "sha256(wuhu-routing-holdout-v1:relative_path)",
            "per_category": per_category,
            "rules": "生成后冻结；不得根据本集失败样本继续修改路由词表。",
        },
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("wuhu_knowledge_base"))
    parser.add_argument("--per-category", type=int, default=2)
    parser.add_argument("--output", type=Path, default=Path("backend/evaluation/routing_holdout.frozen.json"))
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"冻结集已存在，拒绝覆盖：{args.output}")
    payload = build(args.corpus, args.per_category)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "cases": len(payload["cases"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
