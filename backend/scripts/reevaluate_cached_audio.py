"""复用既有 ASR/说话人结果，快速复评工单抽取与部门路由。"""
from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from statistics import mean

from app.config import get_settings
from app.domain.wuhu import seed_wuhu_departments
from app.harness.agents.dept_router import DeptRouter
from app.intake.llm_service import LLMIntakeService
from app.intake.service import IntakeService
from app.llm.deepseek import DeepSeekClient
from app.storage.store import MemoryStore
from scripts.evaluate_synthetic_audio import (
    FIELD_THRESHOLDS,
    build_summary,
    ngram_scores,
    rounded,
    text_similarity,
)


async def run(source: Path, output: Path, mode: str = "rules") -> dict:
    previous = json.loads(source.read_text(encoding="utf-8"))
    settings = get_settings().model_copy(update={
        "storage_mode": "memory", "vector_backend": "memory", "pi_agent_enabled": False,
        **({"deepseek_api_key": ""} if mode == "rules" else {}),
    })
    store = MemoryStore()
    await seed_wuhu_departments(store)
    llm = DeepSeekClient(settings)
    router = DeptRouter(llm, store)
    rules = IntakeService()
    intake = LLMIntakeService(llm, settings, rules) if mode == "production" else rules
    cases = []
    for old in previous["cases"]:
        row = deepcopy(old)
        if row.get("status") != "ok":
            cases.append(row)
            continue
        kwargs = {
            "source_type": "audio", "audio_file_name": Path(row["audio_file"]).name,
            "source_channel": "合成12345双人通话（缓存 ASR 复评）",
        }
        if mode == "production":
            order = await intake.analyze(row["roles"]["formatted_text"], **kwargs)
        else:
            order = intake.analyze(row["roles"]["formatted_text"], **kwargs)
        query = " ".join(filter(None, [
            order.title, order.elements.location, order.elements.event, order.elements.request,
        ]))
        route = await router.route(query)
        expected = row["expected"]
        similarities = {
            "location": text_similarity(expected["location"], order.elements.location),
            "event": text_similarity(expected["event"], order.elements.event),
            "request": text_similarity(expected["request"], order.elements.request),
        }
        passes = {key: similarities[key] >= value for key, value in FIELD_THRESHOLDS.items()}
        support_precision, _, _ = ngram_scores(
            "。".join(t.text for t in order.source.dialogue_turns if t.role == "citizen")
            if order.source.dialogue_turns else order.source.masked_text,
            f"{order.elements.event}。{order.elements.request}",
        )
        ids = route.get("dept_ids", [])
        generation = order.generation.model_dump()
        row["workorder"] = {
            "title": order.title, "region": order.region, "elements": order.elements.model_dump(),
            "generation_mode": generation["mode"], "generation_provider": generation["provider"],
            "generation_model": generation["model"], "fallback_reason": generation["fallback_reason"],
            "quality": order.quality.model_dump(),
            "field_passes": passes,
        }
        row["routing"] = {
            "dept_ids": ids, "dept_names": route.get("dept_names", []),
            "matched_by": route.get("matched_by", ""), "expected_dept_id": expected["department_id"],
        }
        row["metrics"].update({
            "field_completeness": rounded(mean(bool(getattr(order.elements, key)) for key in FIELD_THRESHOLDS)),
            "location_similarity": rounded(similarities["location"]),
            "event_similarity": rounded(similarities["event"]),
            "request_similarity": rounded(similarities["request"]),
            "field_pass_rate": rounded(mean(passes.values())),
            "content_support_precision": rounded(support_precision),
            "region_accuracy": float(expected["region"] == order.region or expected["region"] in order.elements.location),
            "routing_top1": float(bool(ids) and ids[0] == expected["department_id"]),
            "routing_topk": float(expected["department_id"] in ids),
        })
        cases.append(row)

    summary = build_summary(cases, previous["summary"]["manifest_cases"])
    report = {
        "metadata": {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_report": str(source), "mode": f"reuse_cached_asr_{mode}",
            "field_similarity_thresholds": FIELD_THRESHOLDS,
            "notes": ["仅复评工单抽取与路由；ASR/角色指标直接继承源报告。"],
        },
        "summary": summary,
        "cases": cases,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("docs/competition/wuhu_synthetic_audio_evaluation.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/competition/wuhu_synthetic_audio_reevaluation.json"))
    parser.add_argument("--mode", choices=("rules", "production"), default="rules")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.source, args.output, args.mode)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
