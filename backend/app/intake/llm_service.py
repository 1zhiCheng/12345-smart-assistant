"""LLM 优先的诉求理解服务；任何异常都安全回退到规则基线。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.intake.models import StandardWorkOrder, WorkOrderElements, WorkOrderGeneration, WorkOrderQuality
from app.intake.service import FIELD_LABELS, IntakeService
from app.llm.client import ChatMessage, LLMClient, LLMError
from app.utils.logging import get_logger

logger = get_logger(__name__)


class EvidenceItem(BaseModel):
    field: Literal["time", "location", "subjects", "event", "request"]
    quote: str = Field(min_length=1, max_length=500)


class LLMWorkOrderDraft(BaseModel):
    title: str = Field(min_length=2, max_length=80)
    summary: str = Field(min_length=2, max_length=500)
    time: str = Field(default="", max_length=100)
    location: str = Field(default="", max_length=200)
    subjects: list[str] = Field(default_factory=list, max_length=8)
    event: str = Field(default="", max_length=800)
    request: str = Field(default="", max_length=500)
    ambiguities: list[str] = Field(default_factory=list, max_length=8)
    clarification_questions: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=20)


SYSTEM_PROMPT = """你是芜湖市12345热线工单受理助手。请把群众诉求整理为结构化工单，只输出一个 JSON 对象。
硬性要求：
1. 只能使用输入原文中的事实，不得补充、推测政策、责任部门、原因或处理结果。
2. time/location/subjects/event/request 每个非空字段都必须在 evidence 中提供一段可从原文逐字找到的 quote；无法确认就留空。
3. 先逐字提取群众陈述中的事实和明确诉求，再把字段整理为简洁、客观的政务工单语言；evidence.quote 保留对应的群众原话。不得因规范措辞而遗漏对象、行为、问题、影响或群众要求的处理动作。
4. event 只客观描述发生了什么，尽量包含“对象 + 行为/问题 + 影响”；request 只写群众明确提出的处理诉求，存在多个明确动作时应全部保留。不要用“相关问题”“希望处理”等空泛说法替代原文已有的具体信息。
5. 输入可能已按“接线员/群众”标注，必须忽略接线员的提问、引导、复述和承诺，不得将其写入事实或诉求。
6. 信息缺失或指代不明时写入 ambiguities，并生成简短的 clarification_questions。
7. JSON 字段必须完整：title, summary, time, location, subjects, event, request, ambiguities, clarification_questions, evidence。
8. evidence 数组中 field 只能是 time、location、subjects、event、request 之一；ambiguities 和 clarification_questions 不属于 evidence。
示例 JSON：
{"title":"关于某小区噪声的问题","summary":"某小区夜间噪声扰民，群众希望处理。","time":"夜间","location":"某小区","subjects":[],"event":"噪声扰民","request":"希望处理","ambiguities":[],"clarification_questions":[],"evidence":[{"field":"event","quote":"噪声扰民"}]}"""


class LLMIntakeService:
    def __init__(self, llm: LLMClient, settings: Settings, rules: IntakeService | None = None) -> None:
        self.llm = llm
        self.settings = settings
        self.rules = rules or IntakeService()

    async def analyze(self, text: str, source_type: str = "text", audio_file_name: str | None = None,
                      received_at: str | None = None, source_channel: str = "12345热线",
                      raw_transcript: str | None = None) -> StandardWorkOrder:
        fallback = self.rules.analyze(text, source_type, audio_file_name, received_at, source_channel, raw_transcript)
        if not self.settings.intake_llm_enabled:
            return self._fallback(fallback, "大模型工单生成未授权启用")
        if not getattr(self.llm, "api_key", ""):
            return self._fallback(fallback, "未配置大模型 API Key")

        # 只发送脱敏、人工校对后的文本；原始录音转写只留在本地审计。
        if source_type == "audio":
            model_input = self.rules.clean_audio_transcript(self.rules.mask_sensitive(text.strip()))
        else:
            model_input = self.rules.mask_sensitive(self.rules._normalize(text))
        # 在发送前统一芜湖本地 ASR 高频同音词。原始转写仍留在 source 中审计，
        # 模型证据则针对这份已清洗、脱敏、规范化的输入逐字校验。
        model_input = self.rules._normalize_wuhu_place_names(model_input)
        model_input = model_input[: self.settings.intake_llm_max_input_chars]
        try:
            messages = [ChatMessage.system(SYSTEM_PROMPT), ChatMessage.user(f"诉求原文：\n{model_input}")]
            draft = await self._request_draft(messages)
            evidence, ratio = self._validated_evidence(draft, model_input)
            if ratio < self.settings.intake_llm_min_evidence_ratio:
                retry_messages = messages + [ChatMessage.user(
                    "上次各字段的逐字证据覆盖不足。每个非空的 time、location、subjects、event、request "
                    "都必须在 evidence 中给出可从诉求原文逐字找到的 quote；做不到就将该字段留空。"
                    "请只输出修正后的完整 JSON。"
                )]
                corrected = await self._request_draft(retry_messages)
                corrected_evidence, corrected_ratio = self._validated_evidence(corrected, model_input)
                if corrected_ratio > ratio:
                    draft, evidence, ratio = corrected, corrected_evidence, corrected_ratio
            if ratio < self.settings.intake_llm_min_evidence_ratio:
                return self._fallback(fallback, f"模型证据校验未通过（{ratio:.0%}）")
            return self._merge(fallback, draft, evidence, ratio)
        except (LLMError, ValidationError, TypeError, ValueError) as exc:
            logger.warning("intake LLM failed, using rules: %s", exc)
            return self._fallback(fallback, self._safe_reason(exc))

    async def _request_draft(self, messages: list[dict[str, str]]) -> LLMWorkOrderDraft:
        """请求并校验结构化草稿；仅对可纠正的 JSON/schema 问题重试一次。"""
        retry_note = ChatMessage.user(
            "上次输出为空、不完整或结构不合格。请只输出完整 JSON。"
            "evidence.field 只能取 time、location、subjects、event、request，"
            "不要把 ambiguities 或 clarification_questions 放入 evidence。"
        )
        last_error: LLMError | ValidationError | None = None
        for attempt in range(2):
            try:
                payload = await self.llm.complete_json(
                    messages if attempt == 0 else messages + [retry_note],
                    temperature=self.settings.deepseek_temperature,
                    max_tokens=self.settings.deepseek_max_tokens,
                )
                return LLMWorkOrderDraft.model_validate(payload)
            except LLMError as exc:
                if not any(mark in str(exc) for mark in ("无法从模型输出", "JSON 解析失败")):
                    raise
                last_error = exc
            except ValidationError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    @staticmethod
    def _validated_evidence(draft: LLMWorkOrderDraft, source: str) -> tuple[dict[str, list[str]], float]:
        evidence: dict[str, list[str]] = {}
        for item in draft.evidence:
            quote = item.quote.strip()
            if quote and quote in source:
                evidence.setdefault(item.field, []).append(quote)
        populated = [name for name in ("time", "location", "subjects", "event", "request") if getattr(draft, name)]
        ratio = sum(1 for name in populated if evidence.get(name)) / len(populated) if populated else 0.0
        return evidence, ratio

    def _merge(self, base: StandardWorkOrder, draft: LLMWorkOrderDraft,
               evidence: dict[str, list[str]], evidence_ratio: float) -> StandardWorkOrder:
        # 总体证据率只决定模型草稿能否参与融合；每个字段仍需独立证据，
        # 防止一个无依据字段被其它字段的有效证据“带过关”。
        stated_time = draft.time if evidence.get("time") and self.rules._extract_time(draft.time) else ""
        location = (
            self.rules._normalize_wuhu_place_names(draft.location)
            if evidence.get("location") else base.elements.location
        )
        base_region = self.rules._extract_region(base.elements.location)
        draft_region = self.rules._extract_region(location)
        if base.elements.location and (
            not location
            or (base_region and not draft_region)
            or (base_region == draft_region and len(base.elements.location) > len(location))
        ):
            location = base.elements.location
        subjects = draft.subjects if evidence.get("subjects") else base.elements.subjects
        event = draft.event if evidence.get("event") else base.elements.event
        request = draft.request if evidence.get("request") else base.elements.request
        elements = WorkOrderElements(time=stated_time, location=location,
            time_basis="stated" if stated_time else base.elements.time_basis,
            subjects=list(dict.fromkeys(subjects))[:5], event=event, request=request,
            contact_hint=base.elements.contact_hint)
        if not elements.time:
            elements.time = base.elements.time
        missing = [name for name in FIELD_LABELS if not getattr(elements, name)]
        completeness = round((4 - len(missing)) / 4, 2)
        clarity = max(0.0, round(1 - 0.15 * len(draft.ambiguities), 2))
        fidelity = round(evidence_ratio, 2)
        # 标题与摘要由已经逐字段校验、融合后的要素重新生成，避免未验证的
        # 模型 title/summary 将虚构信息重新带回工单。
        base.title = self.rules._title(elements, base.source.masked_text)
        base.summary = self.rules._summary(elements, base.source.masked_text)
        base.elements = elements
        base.region = self.rules._extract_region(elements.location)
        base.content = self.rules._compose_audio_content(elements)
        base.missing_fields = missing
        base.ambiguities = list(dict.fromkeys(draft.ambiguities))
        base.clarification_questions = list(dict.fromkeys(draft.clarification_questions))
        base.quality = WorkOrderQuality(completeness=completeness, fidelity=fidelity, clarity=clarity,
            overall=round(completeness * .4 + fidelity * .4 + clarity * .2, 2),
            warnings=[f"缺少{FIELD_LABELS[name]}" for name in missing] + draft.ambiguities)
        base.generation = WorkOrderGeneration(mode="llm", provider=self.llm.name(),
            model=getattr(self.llm, "model", ""), evidence=evidence)
        return base

    @staticmethod
    def _fallback(order: StandardWorkOrder, reason: str) -> StandardWorkOrder:
        order.generation = WorkOrderGeneration(mode="rule_fallback", fallback_reason=reason)
        if reason not in order.quality.warnings:
            order.quality.warnings.append(reason)
        return order

    @staticmethod
    def _safe_reason(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            return "模型返回的工单结构不合格"
        if isinstance(exc, LLMError):
            return "大模型服务暂时不可用" if any(x in str(exc) for x in ("网络", "请求失败")) else "大模型输出无法解析"
        return "大模型结果校验失败"
