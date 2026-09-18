"""市民自助入口：公开政策问答、脱敏历史办件参考与转营业员审核。"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import require_user
from app.api.schemas import ApiResponse, CitizenChatRequest, CitizenSubmissionRequest
from app.intake.asr import AsrError, AsrService, SUPPORTED_AUDIO_SUFFIXES
from app.intake.service import IntakeService
from app.llm.client import ChatMessage, LLMError

router = APIRouter(prefix="/citizen", tags=["citizen-service"])
_intake = IntakeService()


class CitizenConsultationDraft(BaseModel):
    """市民口语转写的可检索咨询结构，不承担答复或事实判断。"""

    topic: str = Field(default="", max_length=80)
    facts: list[str] = Field(default_factory=list, max_length=6)
    question: str = Field(default="", max_length=300)
    missing: list[str] = Field(default_factory=list, max_length=4)


def _fallback_consultation(text: str) -> str:
    """模型不可用时仍给前端一份稳定、可直接检索的咨询格式。"""
    cleaned = _intake.clean_audio_transcript(text).strip()
    return "\n".join((
        "【咨询主题】待核实的政务咨询",
        f"【已知情况】{cleaned or '语音转写内容为空，请补充文字说明。'}",
        "【希望咨询】请根据上述情况提供可公开的政策依据、办理渠道或下一步建议。",
        "【待补信息】如涉及具体地点、时间或对象，请补充后再咨询。",
    ))


async def _structure_citizen_consultation(container, transcript: str) -> tuple[str, dict]:
    """脱敏后调用已授权 LLM，将 ASR 口语整理为咨询结构；失败安全降级。"""
    source = _intake.mask_sensitive(_intake.clean_audio_transcript(transcript))[:4000]
    fallback = _fallback_consultation(source)
    settings = container.settings
    llm = container.llm
    if not settings.intake_llm_enabled:
        return fallback, {"mode": "rule_fallback", "reason": "大模型咨询整理未启用", "provider": "", "model": ""}
    if not getattr(llm, "api_key", ""):
        return fallback, {"mode": "rule_fallback", "reason": "未配置大模型 API Key", "provider": "", "model": ""}

    system = """你是芜湖12345市民咨询整理助手。把本地语音转写整理成一个 JSON 对象，不要回答问题。
严格要求：
1. 只能使用输入中的事实，不得编造地点、时间、政策、部门、姓名、电话号码、处理结论或责任。
2. topic 用不超过20字的中性主题；facts 为输入中已明确的事实要点；question 为市民真正希望咨询/解决的问题。
3. 信息不清、缺失或疑似 ASR 错词时，只能写入 missing，不能自行补全。
4. 输出字段固定为 topic, facts, question, missing。"""
    try:
        data = await llm.complete_json(
            [ChatMessage.system(system), ChatMessage.user(f"脱敏后的语音转写：\n{source}")],
            temperature=min(settings.deepseek_temperature, 0.2),
            max_tokens=min(settings.deepseek_max_tokens, 700),
        )
        draft = CitizenConsultationDraft.model_validate(data)
        facts = [item.strip() for item in draft.facts if item.strip()]
        topic = draft.topic.strip() or "待核实的政务咨询"
        question = draft.question.strip() or "请根据上述情况提供可公开的政策依据、办理渠道或下一步建议。"
        missing = [item.strip() for item in draft.missing if item.strip()]
        # 防止模型生成了与来源无关的大段内容；结构化的每个事实必须可回溯到转写内容。
        if not facts or any(item not in source for item in facts):
            raise ValueError("模型事实要点无法在转写中核验")
        formatted = "\n".join((
            f"【咨询主题】{topic}",
            "【已知情况】" + "；".join(facts),
            f"【希望咨询】{question}",
            "【待补信息】" + ("；".join(missing) if missing else "无"),
        ))
        return formatted, {"mode": "llm", "reason": "", "provider": llm.name(), "model": getattr(llm, "model", "")}
    except (LLMError, ValidationError, TypeError, ValueError):
        return fallback, {"mode": "rule_fallback", "reason": "大模型整理失败，已使用本地规则", "provider": "", "model": ""}


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    return {normalized[index:index + 2] for index in range(max(0, len(normalized) - 1)) if normalized[index:index + 2].strip()}


def _public_case_summary(row: dict) -> dict:
    """只返回已办结工单的脱敏摘要，绝不暴露录音、原文、联系方式或办理人。"""
    elements = row.get("elements") or {}
    reply = row.get("reply") or {}
    return {
        "case_id": str(row.get("case_id") or row.get("_id") or ""),
        "title": _intake.mask_sensitive(str(row.get("title") or "历史办件"))[:100],
        "summary": _intake.mask_sensitive(str(row.get("summary") or elements.get("event") or ""))[:240],
        "location": _intake.mask_sensitive(str(elements.get("location") or ""))[:120],
        "status": "已办结",
        "reply_summary": _intake.mask_sensitive(str(reply.get("draft") or ""))[:260],
    }


async def _search_public_history(store, query: str) -> list[dict]:
    query_terms = _bigrams(query)
    candidates = []
    for row in await store.find("workorders"):
        if row.get("status") != "confirmed" or row.get("handoff_status") != "completed":
            continue
        safe = _public_case_summary(row)
        corpus = " ".join(str(safe.get(key, "")) for key in ("title", "summary", "location", "reply_summary"))
        terms = _bigrams(corpus)
        score = len(query_terms & terms) / max(1, len(query_terms))
        if score > 0:
            candidates.append((score, safe))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in candidates[:3]]


@router.post("/chat", response_model=ApiResponse)
async def citizen_chat(body: CitizenChatRequest, request: Request):
    """无需登录的市民咨询入口，仅检索公开政策与脱敏、已办结历史摘要。"""
    container = request.app.state.container
    query = _intake.mask_sensitive(body.query.strip())
    history = await _search_public_history(container.store, query)
    # 历史办件仅供市民在前端查看参考，绝不能拼入路由/检索输入；否则历史中
    # 的“出租车、医疗”等词会污染当前事项的承办部门判断和官方证据召回。
    route = await container.dept_router.route(query)
    route_depts = route.get("dept_ids") or None
    session_id = body.session_id or f"citizen-{uuid.uuid4().hex[:16]}"
    result = await container.orchestrator.answer(
        query=query,
        session_id=session_id,
        user_id=f"public:{session_id}",
        dept_ids=route_depts,
    )
    result["route"] = route
    verification = result.get("verification") or {}
    unresolved = (
        result.get("intent_type") == "complaint"
        or not bool(verification.get("passed"))
        or int(result.get("retrieved_count") or 0) == 0
    )
    return ApiResponse(data={
        **result,
        "session_id": session_id,
        "public_history": history,
        "requires_operator_review": unresolved,
        "review_hint": "该事项建议转交营业员核实并生成工单" if unresolved else "已提供公开政策与历史办件参考；如仍未解决可转交营业员。",
    })


@router.post("/transcribe", response_model=ApiResponse)
async def citizen_transcribe(request: Request, file: UploadFile = File(...)):
    """市民语音输入仅在内存中转写，不保存原始录音。"""
    settings = request.app.state.container.settings
    file_name = Path(file.filename or "citizen-recording").name
    if Path(file_name).suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise HTTPException(status_code=415, detail="仅支持 MP3、M4A、WAV、WEBM、OGG 或 MP4 录音。")
    content = await file.read(settings.asr_max_upload_mb * 1024 * 1024 + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="录音文件为空。")
    if len(content) > settings.asr_max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"录音不能超过 {settings.asr_max_upload_mb} MB。")
    try:
        transcript = await AsrService(settings).transcribe(content, file_name, file.content_type)
    except AsrError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raw_text = _intake.mask_sensitive(transcript.text)
    structured_text, generation = await _structure_citizen_consultation(request.app.state.container, raw_text)
    return ApiResponse(data={
        "text": structured_text,
        "raw_text": raw_text,
        "provider": transcript.provider,
        "model": transcript.model,
        "generation": generation,
    })


@router.post("/submissions", response_model=ApiResponse, status_code=201)
async def submit_to_operator(body: CitizenSubmissionRequest, request: Request):
    """市民无法解决时仅提交脱敏文字，营业员后续决定是否正式立案。"""
    now = datetime.now(timezone.utc).isoformat()
    submission_id = f"CIT-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    await request.app.state.container.store.upsert("citizen_submissions", {
        "_id": submission_id,
        "submission_id": submission_id,
        "text": _intake.mask_sensitive(body.text.strip()),
        "session_id": (body.session_id or "")[:100],
        "status": "pending_operator_review",
        "created_at": now,
        "claimed_at": None,
        "claimed_by": None,
    })
    return ApiResponse(message="已提交营业员审核", data={"submission_id": submission_id, "status": "pending_operator_review"})


@router.get("/submissions", response_model=ApiResponse)
async def list_operator_submissions(request: Request, user: dict = Depends(require_user)):
    if user.get("role") not in {"operator", "system_admin"}:
        raise HTTPException(status_code=403, detail="仅营业员或系统管理员可查看市民转人工事项")
    rows = await request.app.state.container.store.find("citizen_submissions")
    rows.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return ApiResponse(data=rows)


@router.post("/submissions/{submission_id}/claim", response_model=ApiResponse)
async def claim_operator_submission(submission_id: str, request: Request, user: dict = Depends(require_user)):
    if user.get("role") not in {"operator", "system_admin"}:
        raise HTTPException(status_code=403, detail="仅营业员或系统管理员可审核市民转人工事项")
    row = await request.app.state.container.store.get("citizen_submissions", submission_id)
    if not row:
        raise HTTPException(status_code=404, detail="未找到该市民提交事项")
    if row.get("status") == "pending_operator_review":
        row.update({"status": "claimed", "claimed_at": datetime.now(timezone.utc).isoformat(), "claimed_by": user["id"]})
        await request.app.state.container.store.upsert("citizen_submissions", row)
    return ApiResponse(data=row)
