from __future__ import annotations

import pytest

from scripts.evaluate_official_audio import assert_public_payload, handling_unit_hit


def test_handling_unit_alias_match() -> None:
    assert handling_unit_hit(["市公安局"], ["芜湖市公安局"])
    assert handling_unit_hit(["12315 中心"], ["芜湖市市场监督管理局"]) is False


def test_public_report_rejects_sensitive_fields() -> None:
    assert_public_payload({
        "id": "official-safe",
        "field_similarity_thresholds": {"location": 0.6, "event": 0.35, "request": 0.4},
        "metrics": {"field_accuracy": 1.0},
    })
    with pytest.raises(ValueError, match="受限字段"):
        assert_public_payload({"id": "official-safe", "raw_transcript": "不可公开"})
