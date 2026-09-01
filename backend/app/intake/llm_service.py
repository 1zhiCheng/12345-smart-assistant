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
3. event 客观描述发生了什么；request 只写群众明确提出的诉求。输入可能已按“接线员/群众”标注，必须忽略接线员的提问、引导、复述和承诺，不得将其写入事实或诉求。
4. 信息缺失或指代不明时写入 ambiguities，并生成简短的 clarification_questions。
5. JSON 字段必须完整：title, summary, time, location, subjects, event, request, ambiguities, clarification_questions, evidence。
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
        model_input = model_input[: self.settings.intake_llm_max_input_chars]
        try:
            messages = [ChatMessage.system(SYSTEM_PROMPT), ChatMessage.user(f"诉求原文：\n{model_input}")]
            try:
                payload = await self.llm.complete_json(
                    messages, temperature=self.settings.deepseek_temperature,
                    max_tokens=self.settings.deepseek_max_tokens,
                )
            except LLMError as exc:
                # DeepSeek 官方说明 JSON 模式偶尔会返回空 content；只对此类格式问题重试一次。
                if not any(mark in str(exc) for mark in ("无法从模型输出", "JSON 解析失败")):
                    raise
                payload = await self.llm.complete_json(
                    messages + [ChatMessage.user("上次输出为空或不完整。请现在只输出完整 JSON，不要解释。")],
                    temperature=self.settings.deepseek_temperature,
                    max_tokens=self.settings.deepseek_max_tokens,
                )
            draft = LLMWorkOrderDraft.model_validate(payload)
            evidence, ratio = self._validated_evidence(draft, model_input)
            if ratio < self.settings.intake_llm_min_evidence_ratio:
                return self._fallback(fallback, f"模型证据校验未通过（{ratio:.0%}）")
            return self._merge(fallback, draft, evidence, ratio)
        except (LLMError, ValidationError, TypeError, ValueError) as exc:
            logger.warning("intake LLM failed, using rules: %s", exc)
            return self._fallback(fallback, self._safe_reason(exc))

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
        stated_time = draft.time if self.rules._extract_time(draft.time) else ""
        elements = WorkOrderElements(time=stated_time, location=draft.location,
            time_basis="stated" if stated_time else base.elements.time_basis,
            subjects=list(dict.fromkeys(draft.subjects))[:5], event=draft.event, request=draft.request,
            contact_hint=base.elements.contact_hint)
        if not elements.time:
            elements.time = base.elements.time
        missing = [name for name in FIELD_LABELS if not getattr(elements, name)]
        completeness = round((4 - len(missing)) / 4, 2)
        clarity = max(0.0, round(1 - 0.15 * len(draft.ambiguities), 2))
        fidelity = round(evidence_ratio, 2)
        base.title = draft.title.strip()
        base.summary = draft.summary.strip()
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
