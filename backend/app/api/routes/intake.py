"""12345 热线端到端工单受理、转派与回复 API。"""
from __future__ import annotations

from datetime import datetime, timezone

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile

from app.api.deps import require_admin, require_user
from app.api.schemas import ApiResponse
from app.intake.models import (
    IntakeAnalyzeRequest,
    IntakeClarifyRequest,
    IntakeConfirmRequest,
    IntakeHandoffResultRequest,
    IntakeSplitRequest,
)
from app.intake.asr import AsrError, AsrService, SUPPORTED_AUDIO_SUFFIXES
from app.intake.service import IntakeService
from app.intake.llm_service import LLMIntakeService
from app.domain.wuhu import CATEGORY_TO_DEPT, DEPARTMENT_NAMES

router = APIRouter(prefix="/intake", tags=["12345-intake"])
service = IntakeService()


async def _reply_evidence(store, citations: list[dict]) -> tuple[list[str], set[str]]:
    """从存储层重新读取引用正文，不能把前端传入的 snippet 当作可信证据。"""
    evidence: list[str] = []
    known_chunk_ids: set[str] = set()
    for citation in citations:
        doc_id = str(citation.get("doc_id", "")).strip()
        try:
            chunk_index = int(citation.get("chunk_index", 0))
        except (TypeError, ValueError):
            continue
        chunk_id = f"{doc_id}:{chunk_index}"
        chunk = await store.get("chunks", chunk_id) if doc_id else None
        if chunk:
            known_chunk_ids.add(chunk_id)
            evidence.append(str(chunk.get("content", "")))
    return evidence, known_chunk_ids


@router.post("/transcribe", response_model=ApiResponse)
async def transcribe_audio(request: Request, file: UploadFile = File(...), user: dict = Depends(require_user)):
    container = request.app.state.container
    settings = container.settings
    file_name = Path(file.filename or "recording").name
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_AUDIO_SUFFIXES:
        raise HTTPException(status_code=415, detail="不支持该录音格式，请上传 MP3、M4A、WAV、WEBM、OGG 或 MP4 文件。")

    limit = settings.asr_max_upload_mb * 1024 * 1024
    content = await file.read(limit + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=400, detail="录音文件为空。")
    if len(content) > limit:
        raise HTTPException(status_code=413, detail=f"录音文件不能超过 {settings.asr_max_upload_mb} MB。")

    try:
        result = await AsrService(settings).transcribe(content, file_name, file.content_type)
    except AsrError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # 先格式化为接线员/群众话轮；工单生成只读取群众话轮，完整对话保留用于审计。
    dialogue = (
        service.format_diarized_segments(result.segments)
        if result.diarization_mode == "acoustic"
        else service.format_audio_dialogue(result.text)
    )
    preview_order = await LLMIntakeService(container.llm, settings, service).analyze(
        dialogue.formatted_text,
        source_type="audio",
        audio_file_name=file_name,
        raw_transcript=result.text,
    )
    return ApiResponse(data={
        "text": dialogue.formatted_text,
        "formatted_text": dialogue.formatted_text,
        "citizen_text": dialogue.citizen_text,
        "turns": [turn.model_dump() for turn in dialogue.turns],
        "role_format_mode": dialogue.mode,
        "raw_text": result.text,
        "file_name": file_name,
        "provider": result.provider,
        "model": result.model,
        "diarization_confidence": result.diarization_confidence,
        "diarization_reason": result.diarization_reason,
        "generation": preview_order.generation.model_dump(),
    })


@router.post("/analyze", response_model=ApiResponse)
async def analyze(req: IntakeAnalyzeRequest, request: Request, user: dict = Depends(require_user)):
    container = request.app.state.container
    workorder = await LLMIntakeService(container.llm, container.settings, service).analyze(
        req.text,
        req.source_type,
        req.audio_file_name,
        req.received_at,
        req.source_channel,
        req.raw_transcript,
    )
    return ApiResponse(data=workorder.model_dump())


@router.post("/clarify", response_model=ApiResponse)
async def clarify(req: IntakeClarifyRequest, request: Request, user: dict = Depends(require_user)):
    answers = req.answers.model_dump(exclude_none=True)
    labels = {"time": "事件时间", "location": "事件地点", "event": "事件经过", "request": "群众诉求", "additional_details": "其他核实信息"}
    supplements = [f"{labels[key]}：{str(value).strip()}" for key, value in answers.items() if str(value).strip()]
    if not supplements:
        raise HTTPException(status_code=400, detail="请至少填写一项追问补充信息。")

    old = req.workorder
    clarified_text = f"{old.source.masked_text}\n受理员追问核实：{'；'.join(supplements)}"
    container = request.app.state.container
    refined = await LLMIntakeService(container.llm, container.settings, service).analyze(
        clarified_text,
        old.source.type,
        old.source.audio_file_name,
        old.source.received_at,
        old.source.source_channel,
        old.source.raw_text,
    )
    refined.case_id = old.case_id
    refined.created_at = old.created_at
    return ApiResponse(data=refined.model_dump())


@router.post("/split", response_model=ApiResponse)
async def split(req: IntakeSplitRequest, user: dict = Depends(require_user)):
    try:
        drafts = service.split_workorder(req.workorder, req.candidate_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=[draft.model_dump() for draft in drafts])


@router.post("/confirm", response_model=ApiResponse)
async def confirm(req: IntakeConfirmRequest, request: Request, user: dict = Depends(require_user)):
    workorder = req.workorder.model_copy(deep=True)
    container = request.app.state.container
    elements = workorder.elements
    routing_query = " ".join(filter(None, [
        workorder.title, elements.location, elements.event, elements.request,
    ]))
    # 营业员确认后立即计算并固化路由，部门管理员无需手工“找工单”。
    route = await container.dept_router.route(routing_query)
    dept_ids = list(route.get("dept_ids") or [])
    workorder.routing = {
        **route,
        "dept_ids": dept_ids,
        "dept_names": [DEPARTMENT_NAMES.get(dept_id, dept_id) for dept_id in dept_ids],
        "top_k": [
            {"dept_id": dept_id, "dept_name": DEPARTMENT_NAMES.get(dept_id, dept_id), "rank": index + 1}
            for index, dept_id in enumerate(dept_ids)
        ],
        "assigned_at": datetime.now(timezone.utc).isoformat(),
        "assigned_by": "auto_router_after_operator_confirmation",
    }
    primary_dept = dept_ids[0] if dept_ids else ""
    category_by_dept = {dept_id: category for category, dept_id in CATEGORY_TO_DEPT.items()}
    workorder.classification = {
        "category": category_by_dept.get(primary_dept, "待人工分类"),
        "confidence": route.get("confidence", 0.0),
        "source": "auto_router_after_operator_confirmation",
        "reasons": route.get("reasons", []),
    }
    workorder.status = "confirmed"
    workorder.confirmed_at = datetime.now(timezone.utc).isoformat()
    workorder.confirmed_by = user["id"]
    workorder.handoff_status = "pending"
    workorder.handed_off_at = workorder.confirmed_at
    workorder.claimed_at = None
    workorder.claimed_by = None
    workorder.completed_at = None
    doc = workorder.model_dump()
    doc["_id"] = workorder.case_id
    await request.app.state.container.store.upsert("workorders", doc)
    return ApiResponse(data=doc)


@router.get("/workorders", response_model=ApiResponse)
async def list_workorders(request: Request, user: dict = Depends(require_user)):
    rows = await request.app.state.container.store.find("workorders")
    return ApiResponse(data=rows)


@router.get("/handoffs", response_model=ApiResponse)
async def list_handoffs(
    request: Request,
    status: str = Query(default="pending", pattern="^(pending|processing|completed|all)$"),
    user: dict = Depends(require_admin),
):
    """处置阶段队列；只包含已经由受理人员人工确认的工单。"""
    query = None if status == "all" else {"handoff_status": status}
    rows = await request.app.state.container.store.find("workorders", query)
    rows = [row for row in rows if row.get("status") == "confirmed"]
    if user.get("role") == "department_admin":
        dept_id = user.get("dept_id")
        rows = [row for row in rows if dept_id and dept_id in ((row.get("routing") or {}).get("dept_ids") or [])]
    return ApiResponse(data=rows)


@router.post("/handoffs/{case_id}/claim", response_model=ApiResponse)
async def claim_handoff(case_id: str, request: Request, user: dict = Depends(require_admin)):
    store = request.app.state.container.store
    doc = await store.get("workorders", case_id)
    if not doc or doc.get("status") != "confirmed":
        raise HTTPException(status_code=404, detail="未找到已确认工单")
    if user.get("role") == "department_admin":
        dept_ids = (doc.get("routing") or {}).get("dept_ids") or []
        if not user.get("dept_id") or user["dept_id"] not in dept_ids:
            raise HTTPException(status_code=403, detail="该工单尚未转派至当前部门")
    state = doc.get("handoff_status", "pending")
    if state == "completed":
        raise HTTPException(status_code=409, detail="该工单已完成分类转派")
    if state == "processing" and doc.get("claimed_by") != user["id"]:
        raise HTTPException(status_code=409, detail="该工单已被其他管理员领取")
    now = datetime.now(timezone.utc).isoformat()
    doc["handoff_status"] = "processing"
    doc["claimed_at"] = doc.get("claimed_at") or now
    doc["claimed_by"] = user["id"]
    await store.upsert("workorders", doc)
    return ApiResponse(data=doc)


@router.post("/handoffs/{case_id}/recommend", response_model=ApiResponse)
async def recommend_handoff(case_id: str, request: Request, user: dict = Depends(require_admin)):
    """生成可人工修改的分类、部门 Top-K、政策依据与回复草稿。"""
    container = request.app.state.container
    store = container.store
    doc = await store.get("workorders", case_id)
    if not doc or doc.get("status") != "confirmed":
        raise HTTPException(status_code=404, detail="未找到已确认工单")

    existing_depts = ((doc.get("routing") or {}).get("dept_ids") or [])
    if user.get("role") == "department_admin":
        dept_id = user.get("dept_id")
        if not dept_id or dept_id not in existing_depts:
            raise HTTPException(status_code=403, detail="该工单尚未转派至当前部门")

    elements = doc.get("elements") or {}
    routing_query = " ".join(filter(None, [
        str(doc.get("title", "")), str(elements.get("location", "")),
        str(elements.get("event", "")), str(elements.get("request", "")),
    ]))
    route = await container.dept_router.route(routing_query)
    if user.get("role") == "department_admin":
        route = {
            "dept_ids": [user["dept_id"]],
            "dept_names": [DEPARTMENT_NAMES.get(user["dept_id"], user["dept_id"])],
            "matched_by": "assigned_scope", "confidence": 1.0,
            "reasons": ["工单已转派至当前部门"],
        }

    dept_ids = route.get("dept_ids") or existing_depts
    rag_prompt = (
        "请依据芜湖市官方政务文档，为以下12345工单形成供部门工作人员审核的办理依据和回复草稿。"
        "不得补写群众未陈述的事实；没有明确政策依据时必须说明需人工核实。\n"
        f"事项地点：{elements.get('location', '')}\n"
        f"事件经过：{elements.get('event', '')}\n"
        f"群众诉求：{elements.get('request', '')}"
    )
    rag = await container.orchestrator.answer(
        rag_prompt,
        session_id=f"handoff_{case_id}",
        user_id=user["id"],
        dept_ids=dept_ids or None,
    )
    resolved_depts = dept_ids or rag.get("dept_ids") or []
    category_by_dept = {dept: category for category, dept in CATEGORY_TO_DEPT.items()}
    primary_dept = resolved_depts[0] if resolved_depts else ""
    classification = {
        "category": category_by_dept.get(primary_dept, "待人工分类"),
        "confidence": route.get("confidence", rag.get("confidence", 0.0)),
        "source": "dept_router", "reasons": route.get("reasons", []),
    }
    routing = {
        **route,
        "dept_ids": resolved_depts,
        "dept_names": [DEPARTMENT_NAMES.get(dept, dept) for dept in resolved_depts],
        "top_k": [
            {"dept_id": dept, "dept_name": DEPARTMENT_NAMES.get(dept, dept), "rank": index + 1}
            for index, dept in enumerate(resolved_depts)
        ],
    }
    citations = rag.get("citations", [])
    reply_draft = str(rag.get("answer", "")).strip()
    reply_mode = str(rag.get("generation_mode") or "rag_llm")
    verification = rag.get("verification") or {}
    if not reply_draft:
        reply_mode = "rule_fallback"
        dept_name = DEPARTMENT_NAMES.get(primary_dept, "相关承办部门")
        titles = list(dict.fromkeys(str(item.get("doc_title", "")).strip() for item in citations if item.get("doc_title")))
        source_note = f"系统已检索《{'》《'.join(titles[:3])}》等官方资料作为办理参考。" if titles else "暂未检索到可直接适用的官方资料。"
        event = str(elements.get("event", "该事项")).strip() or "该事项"
        reply_draft = (
            f"您好，您反映的“{event}”已登记。经初步研判，建议转交{dept_name}核查处理。"
            f"{source_note}具体责任认定、政策适用和处理结果需由承办部门结合现场情况及政策原文人工审核确认。"
        )
        verification = {
            "passed": False, "score": 0.0,
            "issues": ["大模型未返回有效回复正文，当前展示规则降级草稿，必须人工审核"],
        }
    evidence_texts, known_chunk_ids = await _reply_evidence(store, citations)
    case_text = "\n".join(str(elements.get(key, "")) for key in ("time", "location", "event", "request"))
    compliance = container.reply_compliance_gate.evaluate(
        reply_draft,
        citations,
        evidence_texts=evidence_texts,
        case_text=case_text,
        known_chunk_ids=known_chunk_ids,
        upstream_verification=verification,
    )
    recommendation = {
        "case_id": case_id,
        "classification": classification,
        "routing": routing,
        "policy_basis": {
            "citations": citations,
            "verification": verification,
            "retrieved_count": rag.get("retrieved_count", 0),
        },
        "reply": {
            "draft": reply_draft,
            "status": "pending_review" if compliance["automated_passed"] else "blocked",
            "generation_mode": reply_mode,
            "citations": citations,
            "confidence": rag.get("confidence", 0.0),
            "compliance": compliance,
        },
        "release_gate": compliance,
        "requires_human_review": True,
    }
    doc["b_recommendation"] = recommendation
    doc["b_recommended_at"] = datetime.now(timezone.utc).isoformat()
    await store.upsert("workorders", doc)
    return ApiResponse(data=recommendation)


@router.post("/handoffs/{case_id}/result", response_model=ApiResponse)
async def complete_handoff(
    case_id: str,
    body: IntakeHandoffResultRequest,
    request: Request,
    user: dict = Depends(require_admin),
):
    """处置人员回写事项分类、承办部门及可选回复草稿。"""
    store = request.app.state.container.store
    doc = await store.get("workorders", case_id)
    if not doc or doc.get("status") != "confirmed":
        raise HTTPException(status_code=404, detail="未找到已确认工单")
    if doc.get("handoff_status") != "processing":
        raise HTTPException(status_code=409, detail="请先领取工单再回写结果")
    if doc.get("claimed_by") != user["id"] and user.get("role") != "system_admin":
        raise HTTPException(status_code=403, detail="只能处理本人领取的工单")
    if not body.classification or not body.routing:
        raise HTTPException(status_code=400, detail="分类和转派结果不能为空")
    reply = dict(body.reply) if body.reply else None
    if reply:
        draft = str(reply.get("draft") or reply.get("content") or "").strip()
        citations = reply.get("citations") or []
        if not isinstance(citations, list):
            raise HTTPException(status_code=422, detail="回复引用必须为列表")
        evidence_texts, known_chunk_ids = await _reply_evidence(store, citations)
        elements = doc.get("elements") or {}
        case_text = "\n".join(str(elements.get(key, "")) for key in ("time", "location", "event", "request"))
        compliance = request.app.state.container.reply_compliance_gate.evaluate(
            draft,
            citations,
            evidence_texts=evidence_texts,
            case_text=case_text,
            known_chunk_ids=known_chunk_ids,
        )
        if not compliance["can_submit_for_review"]:
            raise HTTPException(status_code=422, detail={
                "message": "回复未通过合规门禁，请修改后重试",
                "issues": compliance["issues"],
                "compliance": compliance,
            })
        reply.update({
            "draft": draft,
            "status": "approved",
            "reviewed_by": user["id"],
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "compliance": compliance,
        })
    doc["classification"] = body.classification
    doc["routing"] = body.routing
    doc["reply"] = reply
    doc["handoff_status"] = "completed"
    doc["completed_at"] = datetime.now(timezone.utc).isoformat()
    await store.upsert("workorders", doc)
    return ApiResponse(data=doc)
