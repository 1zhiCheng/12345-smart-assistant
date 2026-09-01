"""对双人合成通话执行 ASR、角色分离、工单生成和部门路由端到端评测。"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import wave
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from app.config import get_settings
from app.domain.wuhu import CATEGORY_TO_DEPT, seed_wuhu_departments
from app.harness.agents.dept_router import DeptRouter
from app.intake.asr import AsrService
from app.intake.llm_service import LLMIntakeService
from app.intake.service import IntakeService
from app.llm.deepseek import DeepSeekClient
from app.storage.store import MemoryStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "外部合成训练集" / "双人通话"
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "competition" / "wuhu_synthetic_audio_evaluation.json"
ROLE_PATTERN = re.compile(r"^(接线员|群众)\s*[:：]\s*(.+)$")
ROLE_MAP = {"接线员": "operator", "群众": "citizen"}
FIELD_THRESHOLDS = {"location": 0.60, "event": 0.35, "request": 0.40}


def normalize_text(text: str) -> str:
    """CER 使用的归一化：去标签、空白和标点，保留汉字/字母/数字。"""
    text = re.sub(r"(?:接线员|群众|待确认)\s*[:：]", "", text)
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text.lower()))


def edit_distance(left: Iterable[Any], right: Iterable[Any]) -> int:
    a, b = list(left), list(right)
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for index, value_a in enumerate(a, start=1):
        current = [index]
        for j, value_b in enumerate(b, start=1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (value_a != value_b)))
        previous = current
    return previous[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def ngrams(text: str, size: int = 2) -> Counter[str]:
    value = normalize_text(text)
    if len(value) < size:
        return Counter(value)
    return Counter(value[index:index + size] for index in range(len(value) - size + 1))


def ngram_scores(reference: str, hypothesis: str) -> tuple[float, float, float]:
    ref, hyp = ngrams(reference), ngrams(hypothesis)
    overlap = sum((ref & hyp).values())
    recall = overlap / sum(ref.values()) if ref else float(not hyp)
    precision = overlap / sum(hyp.values()) if hyp else float(not ref)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def text_similarity(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    if not ref:
        return float(not hyp)
    if ref in hyp or hyp in ref:
        containment = min(len(ref), len(hyp)) / max(len(ref), len(hyp))
    else:
        containment = 0.0
    _, _, bigram_f1 = ngram_scores(reference, hypothesis)
    return max(containment, bigram_f1, SequenceMatcher(None, ref, hyp).ratio())


def parse_reference(path: Path) -> tuple[list[str], str, str, str]:
    turns: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = ROLE_PATTERN.match(line.strip())
        if match:
            turns.append((ROLE_MAP[match.group(1)], match.group(2).strip()))
    roles = [role for role, _ in turns]
    full = "。".join(text for _, text in turns)
    operator = "。".join(text for role, text in turns if role == "operator")
    citizen = "。".join(text for role, text in turns if role == "citizen")
    return roles, full, operator, citizen


def audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent
    lower, upper = int(index), min(int(index) + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def rounded(value: float) -> float:
    return round(float(value), 4)


def locate_files(root: Path, pattern: str) -> dict[str, Path]:
    return {path.name: path for path in root.rglob(pattern)}


def build_summary(cases: list[dict[str, Any]], total_manifest_cases: int) -> dict[str, Any]:
    completed = [case for case in cases if case.get("status") == "ok"]
    failed = [case for case in cases if case.get("status") != "ok"]
    metric = lambda name: [float(case["metrics"][name]) for case in completed]  # noqa: E731
    modes = Counter(case["workorder"]["generation_mode"] for case in completed)
    category_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in completed:
        category_rows[case["category"]].append(case)

    by_category = {}
    for category, rows in sorted(category_rows.items()):
        by_category[category] = {
            "cases": len(rows),
            "asr_cer": rounded(mean(row["metrics"]["asr_cer"] for row in rows)),
            "role_sequence_exact_rate": rounded(mean(row["metrics"]["role_sequence_exact"] for row in rows)),
            "routing_top1_accuracy": rounded(mean(row["metrics"]["routing_top1"] for row in rows)),
            "routing_topk_accuracy": rounded(mean(row["metrics"]["routing_topk"] for row in rows)),
            "field_pass_rate": rounded(mean(row["metrics"]["field_pass_rate"] for row in rows)),
        }

    return {
        "manifest_cases": total_manifest_cases,
        "evaluated_cases": len(cases),
        "successful_cases": len(completed),
        "failed_cases": len(failed),
        "failed_ids": [case["id"] for case in failed],
        "audio_minutes": rounded(sum(case.get("audio_duration_seconds", 0) for case in cases) / 60),
        "runtime_minutes": rounded(sum(case.get("runtime_seconds", 0) for case in cases) / 60),
        "asr": {
            "cer_mean": rounded(mean(metric("asr_cer"))) if completed else None,
            "cer_median": rounded(median(metric("asr_cer"))) if completed else None,
            "cer_p95": rounded(percentile(metric("asr_cer"), 0.95)) if completed else None,
        },
        "speaker_roles": {
            "acoustic_diarization_accept_rate": rounded(mean(metric("acoustic_diarization_accepted"))) if completed else None,
            "both_roles_present_rate": rounded(mean(metric("both_roles_present"))) if completed else None,
            "five_turn_exact_rate": rounded(mean(metric("role_sequence_exact"))) if completed else None,
            "role_sequence_accuracy": rounded(mean(metric("role_sequence_accuracy"))) if completed else None,
            "operator_cer_mean": rounded(mean(metric("operator_cer"))) if completed else None,
            "citizen_cer_mean": rounded(mean(metric("citizen_cer"))) if completed else None,
        },
        "workorder": {
            "generation_mode_counts": dict(modes),
            "llm_success_rate": rounded(modes.get("llm", 0) / len(completed)) if completed else None,
            "field_completeness": rounded(mean(metric("field_completeness"))) if completed else None,
            "location_similarity": rounded(mean(metric("location_similarity"))) if completed else None,
            "event_similarity": rounded(mean(metric("event_similarity"))) if completed else None,
            "request_similarity": rounded(mean(metric("request_similarity"))) if completed else None,
            "field_pass_rate": rounded(mean(metric("field_pass_rate"))) if completed else None,
            "content_support_precision": rounded(mean(metric("content_support_precision"))) if completed else None,
            "region_accuracy": rounded(mean(metric("region_accuracy"))) if completed else None,
        },
        "routing": {
            "top1_accuracy": rounded(mean(metric("routing_top1"))) if completed else None,
            "topk_accuracy": rounded(mean(metric("routing_topk"))) if completed else None,
            "matched_by": dict(Counter(case["routing"]["matched_by"] for case in completed)),
        },
        "by_category": by_category,
    }


async def evaluate_case(
    item: dict[str, Any], audio_path: Path, transcript_path: Path, asr: AsrService,
    intake: LLMIntakeService | IntakeService, router: DeptRouter, intake_mode: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    reference_roles, reference_full, reference_operator, reference_citizen = parse_reference(transcript_path)
    duration = audio_duration(audio_path)
    try:
        transcript = await asr.transcribe(audio_path.read_bytes(), audio_path.name, "audio/wav")
        dialogue = (
            IntakeService.format_diarized_segments(transcript.segments)
            if transcript.diarization_mode == "acoustic"
            else IntakeService.format_audio_dialogue(transcript.text)
        )
        predicted_roles = [turn.role for turn in dialogue.turns]
        predicted_operator = "。".join(turn.text for turn in dialogue.turns if turn.role == "operator")
        predicted_citizen = "。".join(turn.text for turn in dialogue.turns if turn.role == "citizen")
        kwargs = dict(
            source_type="audio", audio_file_name=audio_path.name, received_at=item.get("acceptedAt"),
            source_channel="合成12345双人通话", raw_transcript=transcript.text,
        )
        if intake_mode == "production":
            order = await intake.analyze(dialogue.formatted_text, **kwargs)
        else:
            order = intake.analyze(dialogue.formatted_text, **kwargs)
        routing_query = " ".join(filter(None, [order.title, order.elements.location, order.elements.event, order.elements.request]))
        routing = await router.route(routing_query)
        expected_dept = CATEGORY_TO_DEPT[item["category"]]

        similarities = {
            "location": text_similarity(item["location"], order.elements.location),
            "event": text_similarity(item["fact"], order.elements.event),
            "request": text_similarity(item["request"], order.elements.request),
        }
        field_passes = {
            field: similarities[field] >= threshold for field, threshold in FIELD_THRESHOLDS.items()
        }
        predicted_content = f"{order.elements.event}。{order.elements.request}"
        support_precision, _, _ = ngram_scores(reference_citizen, predicted_content)
        sequence_distance = edit_distance(reference_roles, predicted_roles)
        sequence_accuracy = max(0.0, 1 - sequence_distance / max(len(reference_roles), len(predicted_roles), 1))
        dept_ids = routing.get("dept_ids", [])
        generation = order.generation.model_dump()
        metrics = {
            "asr_cer": rounded(character_error_rate(reference_full, transcript.text)),
            "acoustic_diarization_accepted": float(transcript.diarization_mode == "acoustic"),
            "both_roles_present": float({"operator", "citizen"}.issubset(set(predicted_roles))),
            "role_sequence_exact": float(predicted_roles == reference_roles),
            "role_sequence_accuracy": rounded(sequence_accuracy),
            "operator_cer": rounded(character_error_rate(reference_operator, predicted_operator)),
            "citizen_cer": rounded(character_error_rate(reference_citizen, predicted_citizen)),
            "field_completeness": rounded(mean(bool(getattr(order.elements, field)) for field in FIELD_THRESHOLDS)),
            "location_similarity": rounded(similarities["location"]),
            "event_similarity": rounded(similarities["event"]),
            "request_similarity": rounded(similarities["request"]),
            "field_pass_rate": rounded(mean(field_passes.values())),
            "content_support_precision": rounded(support_precision),
            "region_accuracy": float(item["region"] == order.region or item["region"] in order.elements.location),
            "routing_top1": float(bool(dept_ids) and dept_ids[0] == expected_dept),
            "routing_topk": float(expected_dept in dept_ids),
        }
        return {
            "id": item["id"], "category": item["category"], "status": "ok",
            "audio_file": str(audio_path.relative_to(PROJECT_ROOT)),
            "transcript_file": str(transcript_path.relative_to(PROJECT_ROOT)),
            "audio_duration_seconds": rounded(duration),
            "runtime_seconds": rounded(time.perf_counter() - started),
            "asr": {
                "provider": transcript.provider, "model": transcript.model, "text": transcript.text,
                "segment_count": len(transcript.segments), "diarization_mode": transcript.diarization_mode,
                "diarization_confidence": rounded(transcript.diarization_confidence),
                "diarization_reason": transcript.diarization_reason,
            },
            "roles": {
                "reference": reference_roles, "predicted": predicted_roles,
                "formatted_text": dialogue.formatted_text, "format_mode": dialogue.mode,
            },
            "expected": {
                "region": item["region"], "location": item["location"], "event": item["fact"],
                "request": item["request"], "department_id": expected_dept,
            },
            "workorder": {
                "title": order.title, "region": order.region, "elements": order.elements.model_dump(),
                "generation_mode": generation["mode"], "generation_provider": generation["provider"],
                "generation_model": generation["model"], "fallback_reason": generation["fallback_reason"],
                "quality": order.quality.model_dump(), "field_passes": field_passes,
            },
            "routing": {
                "dept_ids": dept_ids, "dept_names": routing.get("dept_names", []),
                "matched_by": routing.get("matched_by", ""), "expected_dept_id": expected_dept,
            },
            "metrics": metrics,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "id": item["id"], "category": item["category"], "status": "error",
            "audio_file": str(audio_path.relative_to(PROJECT_ROOT)),
            "audio_duration_seconds": rounded(duration), "runtime_seconds": rounded(time.perf_counter() - started),
            "error_type": type(exc).__name__, "error": str(exc),
        }


def save_report(output: Path, metadata: dict[str, Any], cases: list[dict[str, Any]], total: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"metadata": metadata, "summary": build_summary(cases, total), "cases": cases}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    dataset = args.dataset.resolve()
    manifest_path = dataset / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if args.limit:
        manifest = manifest[: args.limit]
    audio_files = locate_files(dataset, "*.wav")
    transcript_files = locate_files(dataset, "*.txt")
    missing = [item["id"] for item in manifest if item["audioFile"] not in audio_files or item["transcriptFile"] not in transcript_files]
    if missing:
        raise FileNotFoundError(f"缺少音频或转写标注：{', '.join(missing)}")

    settings = get_settings().model_copy(update={"storage_mode": "memory", "vector_backend": "memory", "pi_agent_enabled": False})
    store = MemoryStore()
    await seed_wuhu_departments(store)
    llm = DeepSeekClient(settings)
    rules = IntakeService()
    intake: LLMIntakeService | IntakeService = LLMIntakeService(llm, settings, rules) if args.intake_mode == "production" else rules
    router = DeptRouter(llm, store)
    asr = AsrService(settings)
    metadata = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset": str(dataset.relative_to(PROJECT_ROOT)), "dataset_type": "synthetic_deidentified",
        "intake_mode": args.intake_mode, "asr_provider": settings.asr_provider,
        "asr_model_path": settings.asr_local_model_path,
        "field_similarity_thresholds": FIELD_THRESHOLDS,
        "notes": [
            "该结果只衡量合成双人通话，不等同于官方测试集或真实热线录音成绩。",
            "CER 在去除说话人标签、空白和标点后按字符编辑距离/参考字符数计算。",
            "角色指标基于文本角色归属，不是带人工时间戳标注的标准 DER。",
            "字段通过率是 location/event/request 三项达到公开相似度阈值的平均值。",
            "Top-K 中 K 为路由器本次实际返回的候选部门数量。",
        ],
    }

    cases: list[dict[str, Any]] = []
    total_manifest = len(json.loads(manifest_path.read_text(encoding="utf-8-sig")))
    for index, item in enumerate(manifest, start=1):
        print(f"[{index}/{len(manifest)}] {item['id']} {item['category']}", flush=True)
        result = await evaluate_case(
            item, audio_files[item["audioFile"]], transcript_files[item["transcriptFile"]],
            asr, intake, router, args.intake_mode,
        )
        cases.append(result)
        save_report(args.output.resolve(), metadata, cases, total_manifest)
        if result["status"] == "ok":
            print(
                f"  CER={result['metrics']['asr_cer']:.3f} roles={result['roles']['predicted']} "
                f"route_top1={int(result['metrics']['routing_top1'])} mode={result['workorder']['generation_mode']}",
                flush=True,
            )
        else:
            print(f"  ERROR {result['error_type']}: {result['error']}", flush=True)
    save_report(args.output.resolve(), metadata, cases, total_manifest)
    return build_summary(cases, total_manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--intake-mode", choices=("production", "rules"), default="production")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条；0 表示全量")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
