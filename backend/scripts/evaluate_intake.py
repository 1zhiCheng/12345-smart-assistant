"""12345 工单留出评测：字段质量、信息完整率、事实忠实度与部门路由。"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from app.config import get_settings
from app.domain.wuhu import CATEGORY_TO_DEPT, seed_wuhu_departments
from app.harness.agents.dept_router import DeptRouter
from app.intake.llm_service import LLMIntakeService
from app.intake.service import IntakeService
from app.llm.deepseek import DeepSeekClient
from app.storage.store import MemoryStore

FIELDS = ("time", "location", "event", "request")


def normalize(value: str) -> str:
    return re.sub(r"[\s，,。；;：:！？!?（）()]", "", value).lower()


def field_matches(actual: str, expected_terms: list[str]) -> bool:
    actual_norm = normalize(actual)
    return bool(actual_norm) and all(normalize(term) in actual_norm for term in expected_terms)


def score_case(order, expected: dict[str, list[str]]) -> dict:
    expected_fields = [field for field in FIELDS if expected.get(field)]
    predicted_fields = [field for field in FIELDS if getattr(order.elements, field)]
    correct = [field for field in expected_fields if field_matches(getattr(order.elements, field), expected[field])]
    unsupported = [field for field in predicted_fields if field in expected_fields and field not in correct]
    return {
        "field_accuracy": len(correct) / len(expected_fields) if expected_fields else 1.0,
        "information_completeness": len([f for f in expected_fields if getattr(order.elements, f)]) / len(expected_fields) if expected_fields else 1.0,
        "fact_fidelity": len(correct) / (len(correct) + len(unsupported)) if correct or unsupported else 1.0,
        "correct_fields": correct,
        "incorrect_fields": unsupported,
        "missing_fields": [field for field in expected_fields if not getattr(order.elements, field)],
        "generation_mode": order.generation.mode,
    }


async def evaluate(dataset_path: Path, mode: str) -> dict:
    cases = json.loads(dataset_path.read_text(encoding="utf-8"))
    rules = IntakeService()
    settings = get_settings()
    if mode == "llm":
        llm = DeepSeekClient(settings)
        analyzer = LLMIntakeService(llm, settings, rules)
    else:
        llm = DeepSeekClient(settings.model_copy(update={"deepseek_api_key": ""}))
        analyzer = None
    store = MemoryStore()
    await seed_wuhu_departments(store)
    router = DeptRouter(llm, store)

    details = []
    for case in cases:
        if analyzer:
            order = await analyzer.analyze(case["text"], received_at=case.get("received_at"))
        else:
            order = rules.analyze(case["text"], received_at=case.get("received_at"))
        query = " ".join(filter(None, [order.title, order.elements.location, order.elements.event, order.elements.request]))
        route = await router.route(query)
        expected_dept = CATEGORY_TO_DEPT.get(case.get("official_label", {}).get("category", ""), "")
        ids = route.get("dept_ids", [])
        details.append({
            "id": case["id"], **score_case(order, case["expected"]),
            "expected_department": expected_dept, "predicted_departments": ids,
            "routing_top1": float(bool(expected_dept) and bool(ids) and ids[0] == expected_dept),
            "routing_topk": float(bool(expected_dept) and expected_dept in ids),
            "routing_matched_by": route.get("matched_by", ""),
        })

    metrics = {
        metric: round(sum(row[metric] for row in details) / len(details), 4) if details else 0.0
        for metric in ("field_accuracy", "information_completeness", "fact_fidelity", "routing_top1", "routing_topk")
    }
    return {"mode": mode, "dataset": str(dataset_path), "cases": len(details), "metrics": metrics, "details": details}


def main() -> None:
    parser = argparse.ArgumentParser(description="评测 12345 工单理解与路由质量")
    parser.add_argument("dataset", type=Path, nargs="?", default=Path("evaluation/intake_cases.example.json"))
    parser.add_argument("--mode", choices=("rules", "llm"), default="rules")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.dataset, args.mode))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
