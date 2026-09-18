"""政策回复确定性合规门禁测试。"""
from app.compliance.reply_gate import ReplyComplianceGate


def citation(index: int = 0) -> dict:
    return {
        "doc_id": "doc-policy",
        "doc_title": "芜湖市政务政策文件",
        "dept_id": "dept-test",
        "chunk_index": index,
        "snippet": "申请人应当在30日内提交材料。",
    }


def test_grounded_reply_passes_automated_gate_but_never_auto_publishes():
    result = ReplyComplianceGate().evaluate(
        "申请人应当在30日内提交材料。[来源1]",
        [citation()],
        evidence_texts=["申请人应当在30日内提交材料。"],
        known_chunk_ids={"doc-policy:0"},
        upstream_verification={"passed": True},
    )

    assert result["automated_passed"] is True
    assert result["status"] == "review_required"
    assert result["can_submit_for_review"] is True
    assert result["can_auto_publish"] is False
    assert result["requires_human_review"] is True


def test_gate_blocks_invalid_citation_marker_and_unknown_chunk():
    result = ReplyComplianceGate().evaluate(
        "应当提交材料。[来源2]",
        [citation()],
        evidence_texts=["应当提交材料。"],
        known_chunk_ids=set(),
    )

    assert result["status"] == "blocked"
    assert {item["name"] for item in result["checks"] if not item["passed"]} >= {
        "citation_integrity", "citation_marker_integrity"
    }


def test_gate_blocks_uncited_policy_claim_overpromise_and_unsupported_number():
    result = ReplyComplianceGate().evaluate(
        "根据政策规定，我们保证解决，并在7日内办结。",
        [],
        evidence_texts=["承办部门应当核查处理。"],
    )

    failed = {item["name"] for item in result["checks"] if not item["passed"]}
    assert {"policy_claim_grounding", "numeric_fact_support", "no_overpromise"} <= failed


def test_gate_blocks_personal_phone_and_identity_card():
    result = ReplyComplianceGate().evaluate(
        "请联系13812345678，身份证号340202199001011234。",
        [],
        case_text="13812345678 340202199001011234",
    )

    assert any(item["name"] == "privacy_safe" and not item["passed"] for item in result["checks"])


def test_generic_human_response_does_not_require_policy_citation():
    result = ReplyComplianceGate().evaluate("已收到您的诉求，建议转相关部门核查处理。")

    assert result["automated_passed"] is True
    assert result["fingerprint"] == ReplyComplianceGate().evaluate(
        "已收到您的诉求，建议转相关部门核查处理。"
    )["fingerprint"]


def test_source_marker_at_paragraph_start_supports_all_clauses_in_paragraph():
    result = ReplyComplianceGate().evaluate(
        "[来源1]申请人应当提交材料。办理期限为30日。",
        [citation()],
        evidence_texts=["申请人应当提交材料。办理期限为30日。"],
        known_chunk_ids={"doc-policy:0"},
    )

    assert result["automated_passed"] is True


def test_source_marker_at_end_supports_compound_clause_with_semicolon():
    result = ReplyComplianceGate().evaluate(
        "申请人应当提交材料；办理期限为30日。[来源1]",
        [citation()],
        evidence_texts=["申请人应当提交材料；办理期限为30日。"],
        known_chunk_ids={"doc-policy:0"},
    )

    assert result["automated_passed"] is True


def test_markdown_heading_and_intro_are_not_treated_as_policy_claims():
    result = ReplyComplianceGate().evaluate(
        "根据已入库的官方政务文档，政策依据如下：\n\n**一、补贴标准**\n\n每孩每年3600元。[来源1]",
        [citation()],
        evidence_texts=["每孩每年3600元。"],
        known_chunk_ids={"doc-policy:0"},
    )

    assert result["automated_passed"] is True


def test_source_marker_at_paragraph_end_supports_earlier_sentence():
    result = ReplyComplianceGate().evaluate(
        "依据联动机制调整价格。新价格自2024年9月1日起执行。[来源1]",
        [citation()],
        evidence_texts=["依据联动机制调整价格。新价格自2024年9月1日起执行。"],
        known_chunk_ids={"doc-policy:0"},
    )

    assert result["automated_passed"] is True


def test_no_evidence_disclaimer_does_not_require_citation():
    result = ReplyComplianceGate().evaluate("根据当前材料，未找到明确规定，需人工核实。")

    assert result["automated_passed"] is True


def test_failed_semantic_verification_blocks_draft():
    result = ReplyComplianceGate().evaluate(
        "已收到您的诉求。", upstream_verification={"passed": False}
    )

    assert result["automated_passed"] is False
    assert result["checks"][-1]["name"] == "semantic_verification"
