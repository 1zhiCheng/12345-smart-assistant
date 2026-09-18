"""政策回复的确定性发布门禁。

LLM Verifier 负责语义审校；本模块负责不可绕过的结构与风险检查。门禁通过只表示
草稿可以提交人工审核，不代表允许自动发送给群众。
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Iterable


_SOURCE_MARKER = re.compile(r"\[来源\s*(\d+)\]")
_POLICY_CLAIM = re.compile(
    r"依据|根据.{0,12}(?:规定|政策|条例|办法|通知)|政策规定|条例规定|"
    r"应当|不得|须提交|需要提交|申请条件|办理条件|办理期限|处罚|罚款|补贴标准"
)
_OVERPROMISE = re.compile(
    r"保证(?:解决|办结|通过|赔偿)|肯定(?:解决|办结|处罚|赔偿)|"
    r"一定(?:解决|办结|通过|赔偿)|立即(?:处罚|赔偿)|百分之百"
)
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDENTITY_CARD = re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")
_NUMBER = re.compile(r"(?<!\d)\d+(?:\.\d+)?%?(?!\d)")
_SENTENCE = re.compile(r"[^。！？\n]+[。！？]?|[^。！？\n]+$")
_SAFE_PUBLIC_NUMBERS = {"110", "120", "12315", "12345"}
_NO_EVIDENCE_MARKERS = ("未找到明确", "未找到直接", "无法据此", "未包含该直接依据", "需人工核实")


class ReplyComplianceGate:
    """对草稿实施可复现的硬门禁，并输出可审计的逐项结果。"""

    @staticmethod
    def _citation_key(citation: dict[str, Any]) -> str:
        return f"{citation.get('doc_id', '')}:{citation.get('chunk_index', 0)}"

    @staticmethod
    def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
        return {"name": name, "passed": passed, "detail": detail}

    def evaluate(
        self,
        draft: str,
        citations: Iterable[dict[str, Any]] | None = None,
        *,
        evidence_texts: Iterable[str] | None = None,
        case_text: str = "",
        known_chunk_ids: set[str] | None = None,
        upstream_verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = str(draft or "").strip()
        citation_list = [dict(item) for item in (citations or []) if isinstance(item, dict)]
        evidence = "\n".join(str(item or "") for item in (evidence_texts or []))
        support_text = evidence + "\n" + str(case_text or "")
        checks: list[dict[str, Any]] = []

        checks.append(self._check("non_empty", bool(text), "回复正文非空" if text else "回复正文为空"))

        malformed = [
            index + 1 for index, item in enumerate(citation_list)
            if not str(item.get("doc_id", "")).strip()
            or not str(item.get("doc_title", "")).strip()
            or not str(item.get("snippet", "")).strip()
        ]
        unknown = []
        if known_chunk_ids is not None:
            unknown = [self._citation_key(item) for item in citation_list if self._citation_key(item) not in known_chunk_ids]
        citation_integrity = not malformed and not unknown
        citation_detail = "引用结构及来源有效"
        if malformed:
            citation_detail = f"第 {malformed} 条引用缺少文档、标题或摘录"
        elif unknown:
            citation_detail = f"引用切片不存在：{', '.join(unknown[:3])}"
        checks.append(self._check("citation_integrity", citation_integrity, citation_detail))

        marker_numbers = [int(value) for value in _SOURCE_MARKER.findall(text)]
        invalid_markers = sorted({value for value in marker_numbers if value < 1 or value > len(citation_list)})
        marker_integrity = not invalid_markers
        checks.append(self._check(
            "citation_marker_integrity", marker_integrity,
            "引用编号与引用列表一致" if marker_integrity else f"引用编号越界：{invalid_markers}",
        ))

        ungrounded_claims: list[str] = []
        # 支持两种可靠引用格式：关键句末尾 [来源N]，或整段开头 [来源N]。
        grounding_text = re.sub(r"([。！？])\s*(\[来源\s*\d+\])", r"\2\1", text)
        for line in grounding_text.splitlines() or [grounding_text]:
            stripped_line = line.strip()
            if (
                (stripped_line.startswith("**") and stripped_line.endswith("**"))
                or stripped_line.endswith(("如下：", "如下:"))
            ):
                continue
            paragraph_grounded = bool(_SOURCE_MARKER.search(line))
            for match in _SENTENCE.finditer(line):
                sentence = match.group(0).strip()
                if (
                    sentence and _POLICY_CLAIM.search(sentence)
                    and not paragraph_grounded and not _SOURCE_MARKER.search(sentence)
                    and not any(marker in sentence for marker in _NO_EVIDENCE_MARKERS)
                ):
                    ungrounded_claims.append(sentence[:80])
        claim_grounded = not ungrounded_claims
        checks.append(self._check(
            "policy_claim_grounding", claim_grounded,
            "政策性结论均带来源编号" if claim_grounded else f"无来源政策断言：{ungrounded_claims[0]}",
        ))

        answer_without_markers = _SOURCE_MARKER.sub("", text)
        numeric_tokens = {value for value in _NUMBER.findall(answer_without_markers) if value not in _SAFE_PUBLIC_NUMBERS}
        unsupported_numbers = sorted(value for value in numeric_tokens if value not in support_text)
        checks.append(self._check(
            "numeric_fact_support", not unsupported_numbers,
            "数字事实均可在工单或证据中核对" if not unsupported_numbers else f"证据外数字：{', '.join(unsupported_numbers[:5])}",
        ))

        promises = sorted(set(match.group(0) for match in _OVERPROMISE.finditer(text)))
        checks.append(self._check(
            "no_overpromise", not promises,
            "未发现越权承诺" if not promises else f"疑似越权承诺：{', '.join(promises[:3])}",
        ))

        sensitive = sorted(set(_MOBILE.findall(text) + _IDENTITY_CARD.findall(text)))
        checks.append(self._check(
            "privacy_safe", not sensitive,
            "未发现身份证号或个人手机号" if not sensitive else "回复中含完整身份证号或个人手机号",
        ))

        if upstream_verification is not None:
            verified = bool(upstream_verification.get("passed", False))
            checks.append(self._check(
                "semantic_verification", verified,
                "语义校验通过" if verified else "上游语义校验未通过",
            ))

        issues = [item["detail"] for item in checks if not item["passed"]]
        automated_passed = not issues
        score = round(sum(1 for item in checks if item["passed"]) / max(len(checks), 1), 4)
        fingerprint_payload = {
            "draft": text,
            "citations": [self._citation_key(item) for item in citation_list],
            "checks": checks,
        }
        fingerprint = sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "version": "reply-gate-v1",
            "status": "review_required" if automated_passed else "blocked",
            "automated_passed": automated_passed,
            "can_submit_for_review": automated_passed,
            "can_auto_publish": False,
            "requires_human_review": True,
            "score": score,
            "checks": checks,
            "issues": issues,
            "fingerprint": fingerprint,
        }
