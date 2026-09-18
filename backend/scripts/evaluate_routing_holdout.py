"""只读评测已经冻结的官方文档部门路由留出集。"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path

from app.config import get_settings
from app.domain.wuhu import seed_wuhu_departments
from app.harness.agents.dept_router import DeptRouter
from app.llm.deepseek import DeepSeekClient
from app.llm.embeddings import EmbeddingClient
from app.llm.relay import RelayClient
from app.storage.store import MemoryStore


async def evaluate(dataset: Path) -> dict:
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    settings = get_settings().model_copy(update={
        "deepseek_api_key": "", "embedding_provider": "local", "routing_semantic_enabled": True,
    })
    store = MemoryStore()
    await seed_wuhu_departments(store)
    router = DeptRouter(DeepSeekClient(settings), store, EmbeddingClient(settings, RelayClient(settings)))
    rows = []
    grouped = defaultdict(list)
    for case in payload["cases"]:
        route = await router.route(case["text"])
        ids = route.get("dept_ids", [])
        row = {
            "id": case["id"], "category": case["category"],
            "expected_department": case["department_id"], "predicted_departments": ids,
            "top1": float(bool(ids) and ids[0] == case["department_id"]),
            "topk": float(case["department_id"] in ids), "matched_by": route.get("matched_by", ""),
        }
        rows.append(row)
        grouped[case["category"]].append(row)
    total = max(len(rows), 1)
    return {
        "dataset": str(dataset), "frozen_metadata": payload["metadata"], "cases": len(rows),
        "top1_accuracy": round(sum(row["top1"] for row in rows) / total, 4),
        "topk_accuracy": round(sum(row["topk"] for row in rows) / total, 4),
        "by_category": {
            category: {
                "cases": len(items),
                "top1_accuracy": round(sum(item["top1"] for item in items) / len(items), 4),
                "topk_accuracy": round(sum(item["topk"] for item in items) / len(items), 4),
            }
            for category, items in sorted(grouped.items())
        },
        "details": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("backend/evaluation/routing_holdout.frozen.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/competition/wuhu_routing_holdout_evaluation.json"))
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.dataset))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("cases", "top1_accuracy", "topk_accuracy")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
