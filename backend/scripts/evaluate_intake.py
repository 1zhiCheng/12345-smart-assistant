"""成员 A 工单评测：字段准确率、信息完整率、事实忠实度。"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from app.config import get_settings
from app.intake.llm_service import LLMIntakeService
from app.intake.service import IntakeService
from app.llm.deepseek import DeepSeekClient

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
    if mode == "llm":
        settings = get_settings()
        analyzer = LLMIntakeService(DeepSeekClient(settings), settings, rules)
    else:
        analyzer = None

    details = []
    for case in cases:
        if analyzer:
            order = await analyzer.analyze(case["text"], received_at=case.get("received_at"))
        else:
            order = rules.analyze(case["text"], received_at=case.get("received_at"))
        details.append({"id": case["id"], **score_case(order, case["expected"])})

    metrics = {
        metric: round(sum(row[metric] for row in details) / len(details), 4) if details else 0.0
        for metric in ("field_accuracy", "information_completeness", "fact_fidelity")
    }
    return {"mode": mode, "dataset": str(dataset_path), "cases": len(details), "metrics": metrics, "details": details}


def main() -> None:
    parser = argparse.ArgumentParser(description="评测成员 A 的诉求要素提取质量")
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
