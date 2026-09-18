"""初始化、校验并评测 12345 录音人工标注数据集。"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.domain.wuhu import CATEGORY_TO_DEPT, seed_wuhu_departments
from app.evaluation.audio_annotations import (
    build_official_private_template,
    build_synthetic_annotations,
    score_annotation_dataset,
    validate_annotation_dataset,
)
from app.harness.agents.dept_router import DeptRouter
from app.intake.asr import AsrService
from app.intake.llm_service import LLMIntakeService
from app.intake.service import IntakeService
from app.llm.deepseek import DeepSeekClient
from app.llm.embeddings import EmbeddingClient
from app.storage.store import MemoryStore
from scripts.evaluate_official_audio import align_cases


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SYNTHETIC_ROOT = PROJECT_ROOT / "外部合成训练集" / "双人通话"
DEFAULT_SYNTHETIC_ANNOTATIONS = PROJECT_ROOT / "backend" / "evaluation" / "audio_annotations.synthetic.dev.json"
DEFAULT_OFFICIAL_CASES = PROJECT_ROOT / "backend" / "evaluation" / "intake_cases.official.deidentified.json"
DEFAULT_OFFICIAL_DATASET = PROJECT_ROOT / "12345赛题数据集-信件类别示例工单及录音" / "信件类别示例工单及录音"
DEFAULT_OFFICIAL_PRIVATE = PROJECT_ROOT / "local_evaluation" / "audio_annotations.official.private.json"
DEFAULT_OFFICIAL_PREFILLED = PROJECT_ROOT / "local_evaluation" / "audio_annotations.official.prefilled.private.json"
DEFAULT_PREDICTIONS = PROJECT_ROOT / "docs" / "competition" / "wuhu_synthetic_audio_production_reevaluation.json"
DEFAULT_REPORT = PROJECT_ROOT / "docs" / "competition" / "wuhu_audio_semantic_evaluation.json"
DEFAULT_MODEL = PROJECT_ROOT / "models" / "bge-small-zh-v1.5"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def init_synthetic(args: argparse.Namespace) -> int:
    manifest_path = args.dataset / "manifest.json"
    dataset = build_synthetic_annotations(load_json(manifest_path), args.dataset.resolve(), PROJECT_ROOT)
    validation = validate_annotation_dataset(dataset)
    if not validation["valid"]:
        raise ValueError("生成的合成标注集无效: " + "; ".join(validation["errors"]))
    save_json(args.output, dataset)
    print(json.dumps({"output": str(args.output), **validation}, ensure_ascii=False, indent=2))
    return 0


def init_official(args: argparse.Namespace) -> int:
    aligned = align_cases(args.dataset.resolve(), args.cases.resolve())
    audio_refs = {}
    for item in aligned:
        audio_path = item["audio_path"].resolve()
        try:
            audio_ref = audio_path.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            audio_ref = audio_path.as_posix()
        audio_refs[item["label"]["id"]] = audio_ref
    dataset = build_official_private_template(load_json(args.cases), audio_refs=audio_refs)
    validation = validate_annotation_dataset(dataset, allow_pending=True)
    save_json(args.output, dataset)
    print(json.dumps({
        "output": str(args.output),
        "privacy": "private_local_only_do_not_commit",
        **validation,
    }, ensure_ascii=False, indent=2))
    return 0


def _field_list(value: str) -> list[str]:
    value = str(value or "").strip()
    return [value] if value else []


def _merge_segments(segments: Any) -> list[dict[str, Any]]:
    """把词级 ASR 片段合并成适合人工检查的说话轮次。"""
    merged: list[dict[str, Any]] = []
    for segment in segments or []:
        text = str(getattr(segment, "text", "") or "").strip()
        if not text:
            continue
        role = str(getattr(segment, "role", "unknown") or "unknown")
        if role not in {"operator", "citizen", "unknown"}:
            role = "unknown"
        start = round(float(getattr(segment, "start", 0.0)), 2)
        end = round(float(getattr(segment, "end", start)), 2)
        previous = merged[-1] if merged else None
        can_merge = bool(
            previous
            and previous["speaker"] == role
            and start - previous["end"] <= 1.2
            and end - previous["start"] <= 30
            and len(previous["text"]) + len(text) <= 180
        )
        if can_merge:
            previous["end"] = end
            previous["text"] = (previous["text"] + text).strip()
        else:
            merged.append({"start": start, "end": end, "speaker": role, "text": text})
    return merged


async def prefill_official(args: argparse.Namespace) -> int:
    if args.output.resolve() == args.annotations.resolve():
        raise ValueError("半自动预标注必须输出到新文件，不能覆盖原始私有模板")
    if args.intake_mode == "production" and not args.allow_external_llm:
        raise SystemExit("production 会把脱敏后的 ASR 文本发送给外部 LLM，必须显式添加 --allow-external-llm")

    aligned = align_cases(args.dataset.resolve(), args.cases.resolve())
    aligned_by_id = {item["label"]["id"]: item for item in aligned}
    if args.resume and args.output.exists():
        dataset = load_json(args.output)
    else:
        dataset = load_json(args.annotations)

    settings = get_settings().model_copy(update={
        "storage_mode": "memory",
        "vector_backend": "memory",
        "pi_agent_enabled": False,
        "asr_provider": "faster_whisper",
        "asr_local_device": args.device,
        "asr_local_compute_type": args.compute_type,
        **({"asr_local_model_path": str(args.model_path.resolve())} if args.model_path else {}),
        **(
            {"deepseek_api_key": "", "intake_llm_enabled": False}
            if args.intake_mode == "rules"
            else {"intake_llm_enabled": True}
        ),
    })
    store = MemoryStore()
    await seed_wuhu_departments(store)
    llm = DeepSeekClient(settings)
    rules = IntakeService()
    intake: IntakeService | LLMIntakeService = (
        LLMIntakeService(llm, settings, rules) if args.intake_mode == "production" else rules
    )
    router = DeptRouter(llm, store)
    asr = AsrService(settings)
    category_by_dept = {dept_id: category for category, dept_id in CATEGORY_TO_DEPT.items()}
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    dataset.setdefault("metadata", {})["prefill"] = {
        "created_at": created_at,
        "mode": args.intake_mode,
        "asr_provider": "faster_whisper",
        "status": "in_progress",
        "privacy": "private_local_only",
        "requires_human_approval": True,
    }

    processed = 0
    failures = 0
    total = len(dataset.get("cases") or [])
    for index, case in enumerate(dataset.get("cases") or [], start=1):
        status = (case.get("review") or {}).get("status")
        if status in {"approved", "seeded"}:
            continue
        if args.resume and status == "machine_draft" and (case.get("machine_prefill") or {}).get("status") == "ok":
            continue
        if args.limit and processed >= args.limit:
            break
        item = aligned_by_id.get(case.get("id"))
        if item is None:
            failures += 1
            case["machine_prefill"] = {"status": "error", "error": "audio_alignment_missing"}
            save_json(args.output, dataset)
            continue

        processed += 1
        print(f"[{index}/{total}] 预标注 {case['id']}", flush=True)
        try:
            audio_path: Path = item["audio_path"]
            content_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
            transcript = await asr.transcribe(audio_path.read_bytes(), audio_path.name, content_type)
            dialogue = (
                IntakeService.format_diarized_segments(transcript.segments)
                if transcript.diarization_mode == "acoustic"
                else IntakeService.format_audio_dialogue(transcript.text)
            )
            analyze_kwargs = {
                "source_type": "audio",
                "audio_file_name": audio_path.name,
                "received_at": case.get("received_at"),
                "source_channel": "比赛真实录音半自动预标注",
                "raw_transcript": transcript.text,
            }
            order = (
                await intake.analyze(dialogue.formatted_text, **analyze_kwargs)
                if args.intake_mode == "production"
                else intake.analyze(dialogue.formatted_text, **analyze_kwargs)
            )
            query = " ".join(filter(None, [order.title, order.elements.location, order.elements.event, order.elements.request]))
            route = await router.route(query)
            dept_ids = list(route.get("dept_ids") or [])
            reference = case.setdefault("reference", {})
            suggested_fields = {
                "time": _field_list(order.elements.time),
                "location": _field_list(order.elements.location),
                "event": _field_list(order.elements.event),
                "request": _field_list(order.elements.request),
            }
            suggested_category = category_by_dept.get(dept_ids[0], "") if dept_ids else ""
            suggested_region = order.region
            suggested_department_names = list(route.get("dept_names") or [])
            existing_fields = reference.get("fields") or {}
            reference["transcript"] = dialogue.formatted_text
            reference["segments"] = _merge_segments(transcript.segments)
            # 比赛 Excel 已给出的结构化字段优先级高于机器建议；Agent 只补空值，
            # 机器建议单独保留，方便人工对照而不会覆盖可信标签。
            reference["fields"] = {
                name: list(existing_fields.get(name) or suggested_fields[name])
                for name in ("time", "location", "event", "request")
            }
            reference["category"] = reference.get("category") or suggested_category
            reference["region"] = reference.get("region") or suggested_region
            reference["department_ids"] = list(reference.get("department_ids") or dept_ids)
            reference["department_names"] = list(reference.get("department_names") or suggested_department_names)
            case["review"] = {
                "status": "machine_draft",
                "annotator": "",
                "reviewed_at": None,
                "notes": "机器已预填转写、角色与时间戳；比赛 Excel 已有结构化字段保持不变。必须逐项听录音核对后由人工批准。",
            }
            case["machine_prefill"] = {
                "status": "ok",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "asr_provider": transcript.provider,
                "asr_model": transcript.model,
                "diarization_mode": transcript.diarization_mode,
                "diarization_confidence": round(transcript.diarization_confidence, 4),
                "generation_mode": order.generation.mode,
                "generation_provider": order.generation.provider,
                "routing_matched_by": route.get("matched_by", ""),
                "routing_confidence": route.get("confidence", 0),
                "requires_human_approval": True,
                "suggestion": {
                    "fields": suggested_fields,
                    "category": suggested_category,
                    "region": suggested_region,
                    "department_ids": dept_ids,
                    "department_names": suggested_department_names,
                },
            }
        except Exception as exc:  # noqa: BLE001
            failures += 1
            case.setdefault("review", {})["status"] = "pending"
            case["machine_prefill"] = {
                "status": "error",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "error": type(exc).__name__,
                "message": str(exc),
            }
            print(f"  ERROR {type(exc).__name__}: {exc}", flush=True)
        save_json(args.output, dataset)

    statuses = Counter((case.get("review") or {}).get("status", "missing") for case in dataset.get("cases") or [])
    remaining = statuses.get("pending", 0)
    run_status = "completed_with_errors" if failures else ("partial" if remaining else "completed")
    dataset["metadata"]["prefill"].update({
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": run_status,
        "processed_this_run": processed,
        "failures_this_run": failures,
        "case_statuses": dict(statuses),
    })
    save_json(args.output, dataset)
    print(json.dumps({
        "output": str(args.output),
        "processed": processed,
        "failures": failures,
        "case_statuses": dict(statuses),
        "privacy": "private_local_only_do_not_commit",
    }, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


def validate(args: argparse.Namespace) -> int:
    result = validate_annotation_dataset(load_json(args.annotations), allow_pending=args.allow_pending)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


async def score_async(args: argparse.Namespace) -> int:
    model = args.embedding_model.resolve()
    if not model.exists():
        raise FileNotFoundError(f"本地语义模型不存在: {model}")
    settings = get_settings().model_copy(update={
        "embedding_provider": "local",
        "embedding_model": str(model),
        "embedding_allow_hash_fallback": False,
    })
    embedder = EmbeddingClient(settings)
    report = await score_annotation_dataset(load_json(args.annotations), load_json(args.predictions), embedder)
    if embedder.last_effective_provider != "local":
        raise RuntimeError(f"语义评测必须使用 local embedding，实际为 {embedder.last_effective_provider}")
    report["metadata"]["embedding_provider"] = embedder.last_effective_provider
    report["metadata"]["embedding_model"] = str(model)
    report["metadata"]["embedding_dimension"] = embedder.dim
    save_json(args.output, report)
    print(json.dumps({"output": str(args.output), "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init_syn = sub.add_parser("init-synthetic", help="从合成清单和角色转写生成开发标注集")
    init_syn.add_argument("--dataset", type=Path, default=DEFAULT_SYNTHETIC_ROOT)
    init_syn.add_argument("--output", type=Path, default=DEFAULT_SYNTHETIC_ANNOTATIONS)
    init_syn.set_defaults(handler=init_synthetic)

    init_real = sub.add_parser("init-official", help="生成本地私有的真实录音人工标注模板")
    init_real.add_argument("--cases", type=Path, default=DEFAULT_OFFICIAL_CASES)
    init_real.add_argument("--dataset", type=Path, default=DEFAULT_OFFICIAL_DATASET)
    init_real.add_argument("--output", type=Path, default=DEFAULT_OFFICIAL_PRIVATE)
    init_real.set_defaults(handler=init_official)

    prefill = sub.add_parser("prefill-official", help="本地 ASR 与工单 Agent 生成真实录音机器草稿")
    prefill.add_argument("--annotations", type=Path, default=DEFAULT_OFFICIAL_PRIVATE)
    prefill.add_argument("--cases", type=Path, default=DEFAULT_OFFICIAL_CASES)
    prefill.add_argument("--dataset", type=Path, default=DEFAULT_OFFICIAL_DATASET)
    prefill.add_argument("--output", type=Path, default=DEFAULT_OFFICIAL_PREFILLED)
    prefill.add_argument("--intake-mode", choices=("rules", "production"), default="rules")
    prefill.add_argument("--allow-external-llm", action="store_true")
    prefill.add_argument("--model-path", type=Path)
    prefill.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    prefill.add_argument("--compute-type", default="auto")
    prefill.add_argument("--limit", type=int, default=0)
    prefill.add_argument("--resume", action="store_true")
    prefill.set_defaults(handler=lambda args: asyncio.run(prefill_official(args)))

    check = sub.add_parser("validate", help="校验标注完整性、冻结状态和时间戳")
    check.add_argument("annotations", type=Path)
    check.add_argument("--allow-pending", action="store_true")
    check.set_defaults(handler=validate)

    score = sub.add_parser("score", help="使用本地 BGE 计算 CER、角色、DER 和语义字段指标")
    score.add_argument("--annotations", type=Path, default=DEFAULT_SYNTHETIC_ANNOTATIONS)
    score.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    score.add_argument("--embedding-model", type=Path, default=DEFAULT_MODEL)
    score.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    score.set_defaults(handler=lambda args: asyncio.run(score_async(args)))

    args = parser.parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
