"""对芜湖 12345 知识库执行保守质量门禁，并将低价值材料移入可恢复隔离区。"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


REJECT_TITLE = re.compile(
    r"招聘|拟聘用|采购|招标|中标|成交结果|询价|竞争性磋商|"
    r"预算|决算|三公经费|财政拨款|统计表|"
    r"征求意见稿|公开征求|意见征集|"
    r"培训班|举办.*培训|召开.*培训|服务项目"
    r"|退役军人名录和事迹载入地方志|公民举报危害国家安全行为奖励办法"
    r"|Token经济发展新机遇|征集涉黑涉恶违法犯罪线索"
    r"|推荐芜湖市农业系列和农业工程专业.*专家库成员"
    r"|创新创业大赛"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path("../wuhu_knowledge_base"))
    parser.add_argument("--minimum", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    base = args.base.resolve()
    manifest_path = base / "manifest.jsonl"
    rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = Counter(row["category"] for row in rows)
    rejected: list[dict] = []
    accepted: list[dict] = []
    seen_titles: set[tuple[str, str]] = set()
    for row in rows:
        title_key = (row["category"], row["title"].strip())
        reason = "低价值标题模式" if REJECT_TITLE.search(row["title"]) else ""
        if not reason and title_key in seen_titles:
            reason = "同类别重复标题"
        if reason and counts[row["category"]] - 1 >= args.minimum:
            counts[row["category"]] -= 1
            rejected.append({**row, "quality_reject_reason": reason})
        else:
            accepted.append(row)
            seen_titles.add(title_key)

    print(json.dumps({"accepted": len(accepted), "rejected": len(rejected), "by_category": dict(sorted(counts.items()))}, ensure_ascii=False, indent=2))
    for row in rejected:
        print(f"[隔离] {row['category']}: {row['title']}")
    if not args.apply:
        return
    below = {category: count for category, count in counts.items() if count < args.minimum}
    if below:
        raise SystemExit(f"质量门禁失败，类别不足 {args.minimum} 份：{below}")

    quarantine = base / "_quarantine"
    quarantine.mkdir(exist_ok=True)
    for row in rejected:
        source = base / row["relative_path"]
        target = quarantine / row["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.move(str(source), str(target))
    manifest_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in accepted), encoding="utf-8")
    rejected_path = base / "quality_rejected.jsonl"
    previous_rejected = rejected_path.read_text(encoding="utf-8") if rejected_path.exists() else ""
    rejected_path.write_text(
        previous_rejected + "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rejected), encoding="utf-8"
    )
    quarantined_total = sum(1 for line in rejected_path.read_text(encoding="utf-8").splitlines() if line.strip())
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(accepted),
        "by_category": dict(sorted(counts.items())),
        "official_only": all(".wuhu.gov.cn" in row["source_url"] for row in accepted),
        "quality_gate": {"minimum_per_category": args.minimum, "quarantined": quarantined_total},
    }
    (base / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
