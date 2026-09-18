from __future__ import annotations

import pytest

from app.evaluation.audio_annotations import (
    assert_public_evaluation_report,
    build_official_private_template,
    character_error_rate,
    diarization_error_rate,
    role_sequence_accuracy,
    score_annotation_dataset,
    validate_annotation_dataset,
)


class FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        vocabulary = ("地点", "事件", "诉求")
        vectors = []
        for text in texts:
            vector = [float(token in text) for token in vocabulary]
            if not any(vector):
                vector = [1.0, 1.0, 1.0]
            vectors.append(vector)
        return vectors


def annotation_dataset() -> dict:
    return {
        "metadata": {"schema_version": "audio-annotation.v1", "dataset_id": "dev", "split": "development", "frozen": False},
        "cases": [{
            "id": "case-1", "source_kind": "synthetic", "split": "development", "frozen": False,
            "reference": {
                "transcript": "接线员：您好\n群众：地点事件诉求",
                "segments": [
                    {"speaker": "operator", "text": "您好", "start": None, "end": None},
                    {"speaker": "citizen", "text": "地点事件诉求", "start": None, "end": None},
                ],
                "fields": {"location": ["地点"], "event": ["事件"], "request": ["诉求"]},
                "category": "城市管理", "region": "镜湖区", "department_ids": ["dept-city"], "department_names": [],
            },
            "review": {"status": "approved", "annotator": "tester", "reviewed_at": "2026-09-16T00:00:00+08:00"},
        }],
    }


def prediction_report() -> dict:
    return {
        "metadata": {"dataset_type": "unit-test"},
        "cases": [{
            "id": "case-1", "status": "ok",
            "asr": {"text": "您好地点事件诉求"},
            "roles": {"predicted": ["operator", "citizen"]},
            "workorder": {"elements": {"location": "地点", "event": "事件", "request": "诉求"}},
            "routing": {"dept_ids": ["dept-city"]},
        }],
    }


def test_text_and_role_metrics() -> None:
    assert character_error_rate("群众：芜湖", "芜湖") == 0
    assert character_error_rate("芜湖", "芜") == 0.5
    assert role_sequence_accuracy(["operator", "citizen"], ["operator", "citizen"]) == 1


def test_der_maps_anonymous_speaker_labels() -> None:
    reference = [
        {"speaker": "operator", "start": 0.0, "end": 1.0},
        {"speaker": "citizen", "start": 1.0, "end": 2.0},
    ]
    hypothesis = [
        {"speaker": "speaker_2", "start": 0.0, "end": 1.0},
        {"speaker": "speaker_1", "start": 1.0, "end": 2.0},
    ]
    result = diarization_error_rate(reference, hypothesis, frame_seconds=0.1)
    assert result["available"] is True
    assert result["der"] == 0


def test_validation_rejects_pending_and_duplicate_ids() -> None:
    dataset = annotation_dataset()
    dataset["cases"][0]["review"]["status"] = "pending"
    dataset["cases"].append(dataset["cases"][0].copy())
    result = validate_annotation_dataset(dataset)
    assert result["valid"] is False
    assert any("重复" in error for error in result["errors"])
    assert any("pending" in error for error in result["errors"])


def test_official_private_template_keeps_local_audio_reference() -> None:
    source = [{
        "id": "official-transport-001",
        "received_at": "2026-07-15T08:43:42+08:00",
        "official_label": {"category": "交通运输", "region": "市本级", "handling_units": ["市公安局"]},
        "expected": {"location": ["冰冻街口"], "event": ["公交线路"], "request": ["恢复站点"]},
    }]
    result = build_official_private_template(
        source,
        audio_refs={"official-transport-001": "private/交通运输/260715111208005.mp3"},
    )
    assert result["cases"][0]["audio_ref"].endswith("260715111208005.mp3")


def test_machine_draft_requires_human_approval_before_scoring() -> None:
    dataset = annotation_dataset()
    dataset["cases"][0]["review"]["status"] = "machine_draft"
    pending_result = validate_annotation_dataset(dataset, allow_pending=True)
    strict_result = validate_annotation_dataset(dataset)
    assert pending_result["valid"] is True
    assert pending_result["ready_cases"] == 0
    assert strict_result["valid"] is False
    assert any("machine_draft" in error for error in strict_result["errors"])


@pytest.mark.asyncio
async def test_semantic_scoring_is_public_safe() -> None:
    report = await score_annotation_dataset(annotation_dataset(), prediction_report(), FakeEmbedder())
    assert report["summary"]["field_semantic_f1"] == 1.0
    assert report["summary"]["routing_top1"] == 1.0
    assert report["summary"]["der_available_cases"] == 0
    assert_public_evaluation_report(report)
    with pytest.raises(ValueError, match="受限字段"):
        assert_public_evaluation_report({"raw_transcript": "不可公开"})
