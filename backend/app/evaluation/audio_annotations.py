"""录音人工标注数据集与端到端语义评测。

公开报告只输出匿名样例 ID 和聚合指标，不写入逐字稿、字段正文或音频路径。
"""
from __future__ import annotations

import itertools
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Protocol

from app.domain.wuhu import CATEGORY_TO_DEPT


ROLE_PATTERN = re.compile(r"^(接线员|群众|待确认)\s*[:：]\s*(.+)$")
ROLE_MAP = {"接线员": "operator", "群众": "citizen", "待确认": "unknown"}
VALID_SPEAKERS = {"operator", "citizen", "unknown"}
REQUIRED_FIELDS = ("location", "event", "request")
DEFAULT_SEMANTIC_THRESHOLDS = {"location": 0.72, "event": 0.68, "request": 0.68}
RESTRICTED_PUBLIC_KEYS = {
    "audio_ref", "audio_path", "transcript", "segments", "reference", "predicted_text",
    "gold", "hypothesis", "field_values", "raw_transcript", "formatted_text",
}


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_text(text: str) -> str:
    text = re.sub(r"(?:接线员|群众|待确认)\s*[:：]", "", text or "")
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


def character_error_rate(reference: str, hypothesis: str) -> float | None:
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    if not ref:
        return None
    return edit_distance(ref, hyp) / len(ref)


def role_sequence_accuracy(reference: list[str], hypothesis: list[str]) -> float | None:
    if not reference:
        return None
    distance = edit_distance(reference, hypothesis)
    return max(0.0, 1 - distance / max(len(reference), len(hypothesis), 1))


def parse_role_transcript(text: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        match = ROLE_PATTERN.match(line.strip())
        if match:
            turns.append({
                "speaker": ROLE_MAP[match.group(1)],
                "text": match.group(2).strip(),
                "start": None,
                "end": None,
            })
    return turns


def _speaker_at(segments: list[dict[str, Any]], timestamp: float) -> str | None:
    for item in segments:
        start, end = item.get("start"), item.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and start <= timestamp < end:
            return str(item.get("speaker") or "unknown")
    return None


def diarization_error_rate(
    reference: list[dict[str, Any]], hypothesis: list[dict[str, Any]], frame_seconds: float = 0.05,
) -> dict[str, Any]:
    """按固定帧计算两人通话 DER，并自动寻找最佳说话人标签映射。"""
    timed_ref = [item for item in reference if isinstance(item.get("start"), (int, float)) and isinstance(item.get("end"), (int, float))]
    timed_hyp = [item for item in hypothesis if isinstance(item.get("start"), (int, float)) and isinstance(item.get("end"), (int, float))]
    if not timed_ref or not timed_hyp:
        return {"available": False, "reason": "missing_timestamped_segments"}
    duration = max(max(float(item["end"]) for item in timed_ref), max(float(item["end"]) for item in timed_hyp))
    frames = max(1, math.ceil(duration / frame_seconds))
    ref_labels = sorted({str(item.get("speaker")) for item in timed_ref})
    hyp_labels = sorted({str(item.get("speaker")) for item in timed_hyp})
    candidate_maps: list[dict[str, str]] = []
    if len(hyp_labels) <= len(ref_labels):
        for perm in itertools.permutations(ref_labels, len(hyp_labels)):
            candidate_maps.append(dict(zip(hyp_labels, perm)))
    else:
        for perm in itertools.permutations(hyp_labels, len(ref_labels)):
            candidate_maps.append({hyp: ref for hyp, ref in zip(perm, ref_labels)})
    candidate_maps = candidate_maps or [{}]

    best: dict[str, Any] | None = None
    for mapping in candidate_maps:
        miss = false_alarm = confusion = reference_speech = 0
        for index in range(frames):
            timestamp = (index + 0.5) * frame_seconds
            ref = _speaker_at(timed_ref, timestamp)
            hyp_raw = _speaker_at(timed_hyp, timestamp)
            hyp = mapping.get(hyp_raw, hyp_raw) if hyp_raw else None
            if ref:
                reference_speech += 1
                if hyp is None:
                    miss += 1
                elif hyp != ref:
                    confusion += 1
            elif hyp is not None:
                false_alarm += 1
        denominator = max(reference_speech, 1)
        result = {
            "available": True,
            "der": (miss + false_alarm + confusion) / denominator,
            "miss_rate": miss / denominator,
            "false_alarm_rate": false_alarm / denominator,
            "confusion_rate": confusion / denominator,
            "mapping": mapping,
            "frame_seconds": frame_seconds,
        }
        if best is None or result["der"] < best["der"]:
            best = result
    return best or {"available": False, "reason": "no_frames"}


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def build_synthetic_annotations(manifest: list[dict[str, Any]], dataset_root: Path, project_root: Path) -> dict[str, Any]:
    transcript_files = {path.name: path for path in dataset_root.rglob("*.txt")}
    audio_files = {path.name: path for path in dataset_root.rglob("*.wav")}
    cases: list[dict[str, Any]] = []
    for item in manifest:
        transcript_path = transcript_files.get(str(item.get("transcriptFile")))
        if transcript_path is None:
            raise FileNotFoundError(f"缺少参考转写: {item.get('transcriptFile')}")
        transcript = transcript_path.read_text(encoding="utf-8-sig").strip()
        audio_path = audio_files.get(str(item.get("audioFile")))
        audio_ref = None
        if audio_path:
            try:
                audio_ref = str(audio_path.relative_to(project_root)).replace("\\", "/")
            except ValueError:
                audio_ref = str(audio_path)
        turns = parse_role_transcript(transcript)
        cases.append({
            "id": str(item["id"]),
            "source_kind": "synthetic",
            "split": "development",
            "frozen": False,
            "audio_ref": audio_ref,
            "received_at": item.get("acceptedAt"),
            "reference": {
                "transcript": transcript,
                "segments": turns,
                "fields": {
                    "time": _as_list(item.get("acceptedAt")),
                    "location": _as_list(item.get("location")),
                    "event": _as_list(item.get("fact")),
                    "request": _as_list(item.get("request")),
                },
                "category": item.get("category", ""),
                "region": item.get("region", ""),
                "department_ids": [CATEGORY_TO_DEPT[item["category"]]] if item.get("category") in CATEGORY_TO_DEPT else [],
                "department_names": _as_list(item.get("department")),
            },
            "review": {
                "status": "seeded",
                "annotator": "synthetic-manifest",
                "reviewed_at": now_iso(),
                "notes": "合成开发集参考标注，不能作为真实热线泛化成绩。",
            },
        })
    return {
        "metadata": {
            "schema_version": "audio-annotation.v1",
            "dataset_id": "wuhu-synthetic-audio-development",
            "source_kind": "synthetic",
            "split": "development",
            "frozen": False,
            "created_at": now_iso(),
            "privacy": "deidentified_synthetic",
            "notes": [
                "该数据集用于开发评测，不替代比赛真实录音冻结集。",
                "参考转写有角色标签但无人工时间戳，因此可以计算 CER 和角色序列，不能计算 DER。",
            ],
        },
        "cases": cases,
    }


def build_official_private_template(
    cases_source: list[dict[str, Any]],
    audio_refs: dict[str, str] | None = None,
) -> dict[str, Any]:
    audio_refs = audio_refs or {}
    cases = []
    for item in cases_source:
        label = item.get("official_label") or {}
        cases.append({
            "id": item["id"],
            "source_kind": "official_real_audio",
            "split": "test",
            "frozen": True,
            # 仅写入本地私有模板；公开评测报告会继续剔除音频路径。
            "audio_ref": audio_refs.get(item["id"]),
            "received_at": item.get("received_at"),
            "reference": {
                "transcript": "",
                "segments": [],
                "fields": {name: _as_list((item.get("expected") or {}).get(name)) for name in ("time", *REQUIRED_FIELDS)},
                "category": label.get("category", ""),
                "region": label.get("region", ""),
                "department_ids": [],
                "department_names": _as_list(label.get("handling_units")),
            },
            "review": {
                "status": "pending",
                "annotator": "",
                "reviewed_at": None,
                "notes": "只在本地填写脱敏逐字稿和说话人时间戳，不提交原始录音或身份信息。",
            },
        })
    return {
        "metadata": {
            "schema_version": "audio-annotation.v1",
            "dataset_id": "wuhu-official-audio-frozen-private",
            "source_kind": "official_real_audio",
            "split": "test",
            "frozen": True,
            "created_at": now_iso(),
            "privacy": "private_local_only",
            "notes": ["冻结测试集；完成标注前不得据此调规则、提示词或阈值。"],
        },
        "cases": cases,
    }


def validate_annotation_dataset(dataset: dict[str, Any], allow_pending: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    metadata = dataset.get("metadata") or {}
    cases = dataset.get("cases")
    if metadata.get("schema_version") != "audio-annotation.v1":
        errors.append("metadata.schema_version 必须为 audio-annotation.v1")
    if not isinstance(cases, list) or not cases:
        errors.append("cases 必须是非空数组")
        cases = []
    if metadata.get("split") == "test" and metadata.get("frozen") is not True:
        errors.append("test 数据集必须 frozen=true")
    ids = [case.get("id") for case in cases]
    duplicates = [case_id for case_id, count in Counter(ids).items() if case_id and count > 1]
    if duplicates:
        errors.append(f"样例 ID 重复: {', '.join(sorted(duplicates))}")
    ready = 0
    timestamped = 0
    for case in cases:
        case_id = str(case.get("id") or "<missing>")
        review = case.get("review") or {}
        status = review.get("status")
        if status not in {"pending", "machine_draft", "seeded", "approved", "rejected"}:
            errors.append(f"{case_id}: review.status 非法")
        if status in {"pending", "machine_draft"} and not allow_pending:
            errors.append(f"{case_id}: 标注仍为 {status}")
        if status in {"seeded", "approved"}:
            ready += 1
            reference = case.get("reference") or {}
            transcript = str(reference.get("transcript") or "").strip()
            if not transcript:
                errors.append(f"{case_id}: 缺少参考逐字稿")
            fields = reference.get("fields") or {}
            for field in REQUIRED_FIELDS:
                if not _as_list(fields.get(field)):
                    errors.append(f"{case_id}: 缺少字段 {field}")
            segments = reference.get("segments") or []
            previous_end = 0.0
            has_timestamps = bool(segments)
            for index, segment in enumerate(segments):
                speaker = segment.get("speaker")
                if speaker not in VALID_SPEAKERS:
                    errors.append(f"{case_id}: segment[{index}] speaker 非法")
                start, end = segment.get("start"), segment.get("end")
                if (start is None) != (end is None):
                    errors.append(f"{case_id}: segment[{index}] 时间戳必须同时填写 start/end")
                if start is None:
                    has_timestamps = False
                elif not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or start < 0 or end <= start:
                    errors.append(f"{case_id}: segment[{index}] 时间戳非法")
                elif start < previous_end:
                    errors.append(f"{case_id}: segment[{index}] 与前一段重叠或乱序")
                elif end is not None:
                    previous_end = float(end)
            if has_timestamps:
                timestamped += 1
            elif segments:
                warnings.append(f"{case_id}: 有角色标注但没有时间戳，DER 不可计算")
    return {
        "valid": not errors,
        "cases": len(cases),
        "ready_cases": ready,
        "timestamped_cases": timestamped,
        "errors": errors,
        "warnings": warnings,
    }


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left)) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right)) or 1.0
    return numerator / (left_norm * right_norm)


def _split_clauses(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[。！？；;\n]+", text or "") if part.strip()]
    return parts or ([text.strip()] if text and text.strip() else [])


def _field_semantic_metrics(
    gold_items: list[str], predicted: str, vector_map: dict[str, list[float]], threshold: float,
) -> dict[str, float]:
    predicted_units = _split_clauses(predicted)
    if not gold_items:
        return {"precision": float(not predicted_units), "recall": 1.0, "f1": float(not predicted_units), "mean_similarity": 1.0}
    if not predicted_units:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "mean_similarity": 0.0}
    gold_best = [max(_cosine(vector_map[gold], vector_map[unit]) for unit in predicted_units) for gold in gold_items]
    predicted_best = [max(_cosine(vector_map[unit], vector_map[gold]) for gold in gold_items) for unit in predicted_units]
    recall = sum(value >= threshold for value in gold_best) / len(gold_best)
    precision = sum(value >= threshold for value in predicted_best) / len(predicted_best)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "mean_similarity": mean(gold_best),
    }


def _round_metrics(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _round_metrics(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_metrics(item) for item in value]
    return value


async def score_annotation_dataset(
    dataset: dict[str, Any], predictions: dict[str, Any], embedder: Embedder,
    semantic_thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    validation = validate_annotation_dataset(dataset)
    if not validation["valid"]:
        raise ValueError("标注集未通过校验: " + "; ".join(validation["errors"][:5]))
    thresholds = semantic_thresholds or DEFAULT_SEMANTIC_THRESHOLDS
    prediction_map = {item.get("id"): item for item in predictions.get("cases", []) if item.get("status") == "ok"}
    usable = [case for case in dataset["cases"] if (case.get("review") or {}).get("status") in {"seeded", "approved"}]
    missing = [case["id"] for case in usable if case["id"] not in prediction_map]
    if missing:
        raise ValueError(f"预测报告缺少 {len(missing)} 条样例: {', '.join(missing[:5])}")

    texts: list[str] = []
    prepared: list[dict[str, Any]] = []
    for case in usable:
        prediction = prediction_map[case["id"]]
        fields = (case["reference"] or {}).get("fields") or {}
        predicted_fields = ((prediction.get("workorder") or {}).get("elements") or {})
        row_fields: dict[str, Any] = {}
        for field in REQUIRED_FIELDS:
            gold = _as_list(fields.get(field))
            predicted = str(predicted_fields.get(field) or "")
            units = _split_clauses(predicted)
            texts.extend(gold)
            texts.extend(units)
            row_fields[field] = {"gold": gold, "predicted": predicted}
        prepared.append({"case": case, "prediction": prediction, "fields": row_fields})
    unique_texts = list(dict.fromkeys(text for text in texts if text))
    vectors = await embedder.embed(unique_texts)
    if len(vectors) != len(unique_texts):
        raise RuntimeError("语义向量数量与文本数量不一致")
    vector_map = dict(zip(unique_texts, vectors))

    rows: list[dict[str, Any]] = []
    for item in prepared:
        case, prediction = item["case"], item["prediction"]
        reference = case["reference"]
        field_metrics = {
            field: _field_semantic_metrics(
                values["gold"], values["predicted"], vector_map, thresholds[field],
            )
            for field, values in item["fields"].items()
        }
        ref_roles = [segment["speaker"] for segment in reference.get("segments", [])]
        hyp_roles = list((prediction.get("roles") or {}).get("predicted") or [])
        ref_transcript = str(reference.get("transcript") or "")
        hyp_transcript = str((prediction.get("asr") or {}).get("text") or "")
        der = diarization_error_rate(reference.get("segments") or [], (prediction.get("asr") or {}).get("segments") or [])
        expected_depts = set(reference.get("department_ids") or [])
        predicted_depts = list((prediction.get("routing") or {}).get("dept_ids") or [])
        top1 = None if not expected_depts else float(bool(predicted_depts) and predicted_depts[0] in expected_depts)
        topk = None if not expected_depts else float(any(dept in expected_depts for dept in predicted_depts))
        rows.append({
            "id": case["id"],
            "source_kind": case["source_kind"],
            "metrics": {
                "cer": character_error_rate(ref_transcript, hyp_transcript),
                "role_sequence_accuracy": role_sequence_accuracy(ref_roles, hyp_roles),
                "both_roles_present": float({"operator", "citizen"}.issubset(set(hyp_roles))),
                "der": der.get("der") if der.get("available") else None,
                "der_status": "available" if der.get("available") else der.get("reason"),
                "field_semantic_f1": mean(metric["f1"] for metric in field_metrics.values()),
                "field_semantic_recall": mean(metric["recall"] for metric in field_metrics.values()),
                "field_support_precision": mean(metric["precision"] for metric in field_metrics.values()),
                "routing_top1": top1,
                "routing_topk": topk,
            },
            "field_metrics": field_metrics,
        })

    def average(name: str) -> float | None:
        values = [row["metrics"][name] for row in rows if row["metrics"].get(name) is not None]
        return mean(values) if values else None

    report = {
        "metadata": {
            "schema_version": "audio-semantic-evaluation.v1",
            "created_at": now_iso(),
            "annotation_dataset_id": (dataset.get("metadata") or {}).get("dataset_id"),
            "prediction_dataset_type": (predictions.get("metadata") or {}).get("dataset_type"),
            "semantic_thresholds": thresholds,
            "privacy": "public_safe_no_transcripts_audio_paths_or_field_text",
            "notes": [
                "语义字段 F1 使用本地真实 embedding，不允许 hash 降级。",
                "没有人工说话人时间戳时 DER 标记为不可用，不用角色序列准确率冒充 DER。",
                "开发集成绩不能视为比赛真实录音泛化成绩。",
            ],
        },
        "summary": {
            "annotation_cases": len(usable),
            "scored_cases": len(rows),
            "cer_mean": average("cer"),
            "role_sequence_accuracy": average("role_sequence_accuracy"),
            "both_roles_present_rate": average("both_roles_present"),
            "der_mean": average("der"),
            "der_available_cases": sum(row["metrics"]["der"] is not None for row in rows),
            "field_semantic_f1": average("field_semantic_f1"),
            "field_semantic_recall": average("field_semantic_recall"),
            "field_support_precision": average("field_support_precision"),
            "routing_top1": average("routing_top1"),
            "routing_topk": average("routing_topk"),
        },
        "cases": rows,
    }
    assert_public_evaluation_report(report)
    return _round_metrics(report)


def assert_public_evaluation_report(payload: Any, trail: tuple[str, ...] = ()) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in RESTRICTED_PUBLIC_KEYS:
                raise ValueError(f"公开评测包含受限字段: {'.'.join((*trail, key))}")
            assert_public_evaluation_report(value, (*trail, key))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            assert_public_evaluation_report(item, (*trail, str(index)))
