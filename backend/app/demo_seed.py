"""用官方文档与脱敏评测结果构建可追溯的离线演示运行态。"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.domain.wuhu import CATEGORY_TO_DEPT, DEPARTMENT_NAMES
from app.pipeline.chunker import Chunker
from app.pipeline.parser import DocumentParser


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_ROOT = PROJECT_ROOT / "wuhu_knowledge_base"
KNOWLEDGE_MANIFEST = KNOWLEDGE_ROOT / "manifest.jsonl"
EVALUATION_REPORT = PROJECT_ROOT / "docs" / "competition" / "wuhu_synthetic_audio_evaluation.json"
SYNTHETIC_MANIFEST = PROJECT_ROOT / "外部合成训练集" / "双人通话" / "manifest.json"
DEMO_TAG = "demo_seed_from_official_docs_and_deidentified_evaluation"
CHINA_TZ = timezone(timedelta(hours=8))


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _iso(value: str | None, fallback: datetime) -> str:
    if not value:
        return fallback.isoformat(timespec="seconds")
    candidate = value.strip().replace(" ", "T")
    return candidate if "+" in candidate or candidate.endswith("Z") else candidate + "+08:00"


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def _load_knowledge_manifest() -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in KNOWLEDGE_MANIFEST.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except (OSError, ValueError, TypeError):
        return []


async def _seed_documents(store, rows: list[dict[str, Any]]):
    parser, chunker = DocumentParser(), Chunker()
    docs_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    chunks_by_dept: dict[str, list[dict[str, Any]]] = defaultdict(list)
    root = KNOWLEDGE_ROOT.resolve()
    now = datetime.now(CHINA_TZ)
    for row in rows:
        category = str(row.get("category", ""))
        dept_id = CATEGORY_TO_DEPT.get(category)
        relative_path = str(row.get("relative_path", ""))
        path = (root / relative_path).resolve()
        if not dept_id or not path.is_file() or root not in path.parents:
            continue
        try:
            drafts = chunker.chunk(parser.parse(path))
        except Exception:
            continue
        doc_id = _stable_id("demo_doc", relative_path)
        created_at = _iso(row.get("crawled_at"), now)
        doc = {
            "_id": doc_id, "dept_id": dept_id, "title": row.get("title") or path.stem,
            "doc_type": "official_government_document", "version": str(row.get("published_at") or "现行"),
            "status": "active", "effective_date": row.get("published_at") or None,
            "source": {"file_name": path.name, "url": row.get("source_url", ""),
                       "publisher": row.get("department", DEPARTMENT_NAMES.get(dept_id, "")),
                       "provenance": "芜湖市政府部门官网公开资料", "demo_seed": True},
            "tags": [category, str(row.get("department", "")), "官方公开资料", "演示数据源"],
            "chunk_count": len(drafts), "vector_status": "ready",
            "pipeline_stages": [
                {"key": "parse", "name": "解析", "done": True},
                {"key": "clean", "name": "清洗", "done": True},
                {"key": "metadata", "name": "元数据", "done": True},
                {"key": "chunk", "name": "切片", "done": True, "detail": f"{len(drafts)}片"},
                {"key": "embed", "name": "向量化", "done": True},
                {"key": "relation", "name": "关系扫描", "done": True},
            ],
            "demo_seed": True, "demo_source": DEMO_TAG, "created_at": created_at, "updated_at": created_at,
        }
        await store.insert_document(doc)
        docs_by_category[category].append(doc)
        chunks = []
        for draft in drafts:
            chunk_id = f"{doc_id}_chunk_{draft['chunk_index']:04d}"
            chunk = {
                **draft, "_id": chunk_id, "doc_id": doc_id, "dept_id": dept_id, "embedding_id": chunk_id,
                "keywords": [category, str(row.get("department", "")), str(row.get("title", ""))[:40]],
                "metadata": {**draft.get("metadata", {}), "source_url": row.get("source_url", ""), "demo_seed": True},
            }
            chunks.append(chunk)
            chunks_by_dept[dept_id].append(chunk)
        await store.insert_chunks(chunks)
    # 同类别文档建立最小可解释关系，供知识治理页展示来源之间的横向关联。
    # 这里只声明“同主题参考”，不推断替代、冲突或效力关系。
    for category, docs in docs_by_category.items():
        dept_id = CATEGORY_TO_DEPT[category]
        for index in range(1, min(len(docs), 4)):
            previous, current = docs[index - 1], docs[index]
            await store.insert_relation({
                "_id": _stable_id("demo_relation", f"{previous['_id']}:{current['_id']}"),
                "from_doc": previous["_id"], "to_doc": current["_id"],
                "from_dept": dept_id, "to_dept": dept_id,
                "relation_type": "reference", "confidence": 1.0,
                "description": f"{category}同主题官方公开资料的检索关联",
                "status": "active", "demo_seed": True, "demo_source": DEMO_TAG,
                "created_at": now.isoformat(timespec="seconds"),
            })
    return docs_by_category, chunks_by_dept


def _quality_score(category_row: dict[str, Any]) -> float:
    cer = float(category_row.get("asr_cer", 1.0))
    roles = float(category_row.get("role_sequence_exact_rate", 0.0))
    route = float(category_row.get("routing_topk_accuracy", 0.0))
    fields = float(category_row.get("field_pass_rate", 0.0))
    return max(0.0, min(1.0, .2 * (1 - cer) + .2 * roles + .35 * route + .25 * fields))


async def _seed_reviews(store, docs_by_category, evaluation: dict[str, Any]) -> int:
    category_metrics = (evaluation.get("summary") or {}).get("by_category") or {}
    now = datetime.now(CHINA_TZ)
    order_count = 0
    for category, dept_id in CATEGORY_TO_DEPT.items():
        score = _quality_score(category_metrics.get(category, {}))
        reviewed_total = reviewed_correct = 0
        for index, doc in enumerate(docs_by_category.get(category, [])[:3]):
            chunks = await store.list_chunks_by_doc(doc["_id"])
            citation = [{"doc_id": doc["_id"], "doc_title": doc["title"], "chunk_index": 0,
                         "section_path": chunks[0].get("section_path", []) if chunks else []}]
            published = doc.get("effective_date") or "文档未标注发布日期"
            qa_pairs = [
                {"question": f"《{doc['title']}》由哪个部门发布或维护？", "expected": DEPARTMENT_NAMES[dept_id],
                 "answer": DEPARTMENT_NAMES[dept_id], "citations": citation, "confidence": .94},
                {"question": f"《{doc['title']}》是否完成知识库切分？", "expected": "已完成解析、切片和向量化",
                 "answer": f"已完成，共形成 {doc['chunk_count']} 个可追溯切片。", "citations": citation, "confidence": .91},
                {"question": "该文档的公开发布日期是什么？", "expected": str(published),
                 "answer": str(published), "citations": citation, "confidence": .89},
            ]
            pending = index == 2
            correct_target = max(0, min(3, round(score * 3)))
            for qa_index, pair in enumerate(qa_pairs):
                correct = qa_index < correct_target
                pair.update({"trace_id": f"demo_review_trace_{category}_{index}_{qa_index}",
                             "verdict": None if pending else ("approved" if correct else "rejected"),
                             "correct": None if pending else correct,
                             "correction": "需核对原文适用范围后再答复" if not pending and not correct else ""})
            created_at = (now - timedelta(days=9 - index)).isoformat(timespec="seconds")
            await store.insert_review_order({
                "_id": _stable_id("demo_review", f"{dept_id}:{doc['_id']}"), "dept_id": dept_id,
                "doc_id": doc["_id"], "doc_title": doc["title"], "status": "pending" if pending else "reviewed",
                "qa_pairs": qa_pairs, "total": 3, "correct": 0 if pending else correct_target,
                "accuracy": None if pending else round(correct_target / 3, 4), "loop_phase_at_create": "human_in_loop",
                "sampled_review": False, "created_at": created_at,
                "reviewed_at": None if pending else (now - timedelta(days=7 - index)).isoformat(timespec="seconds"),
                "reviewed_by": None if pending else "demo_department_reviewer",
                "demo_seed": True, "demo_source": DEMO_TAG,
            })
            order_count += 1
            if not pending:
                reviewed_total += 3
                reviewed_correct += correct_target
                for qa_index, pair in enumerate(qa_pairs):
                    await store.insert_test_question({
                        "_id": _stable_id("demo_question", f"{doc['_id']}:{qa_index}"), "dept_id": dept_id,
                        "doc_id": doc["_id"], "doc_title": doc["title"], "question": pair["question"],
                        "expected": pair["expected"], "answer": pair["answer"], "verdict": pair["verdict"],
                        "correct": pair["correct"], "created_at": created_at, "demo_seed": True,
                    })
        dept = await store.get_department(dept_id)
        if dept:
            accuracy = reviewed_correct / max(reviewed_total, 1)
            dept["review_stats"] = {"total": reviewed_total, "correct": reviewed_correct, "accuracy": round(accuracy, 4)}
            dept["loop_phase"] = "human_on_loop" if reviewed_total and accuracy >= .8 else "human_in_loop"
            dept["admin_users"] = ["cgj_admin"] if dept_id == "dept_city_management" else dept.get("admin_users", [])
            dept["evaluation_basis"] = "36条脱敏合成双人通话 + 官方文档审核演示"
            dept["demo_seed"] = True
            await store.upsert_department(dept)
    return order_count


async def _seed_runtime(store, evaluation, synthetic_rows, chunks_by_dept) -> dict[str, int]:
    cases = evaluation.get("cases") or []
    synthetic_by_id = {str(row.get("id")): row for row in synthetic_rows}
    issues_by_category: dict[str, list[str]] = defaultdict(list)
    for row in synthetic_rows:
        issues_by_category[str(row.get("category", ""))].append(str(row.get("issue", "群众诉求")))
    now = datetime.now(CHINA_TZ)
    counts = defaultdict(int)
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or f"case-{index}")
        expected, metrics = case.get("expected") or {}, case.get("metrics") or {}
        category = str(case.get("category", ""))
        dept_id = CATEGORY_TO_DEPT.get(category, "")
        source = synthetic_by_id.get(case_id, {})
        available_chunks = chunks_by_dept.get(dept_id, [])
        chunk = available_chunks[index % len(available_chunks)] if available_chunks else None
        citation = [] if not chunk else [{"doc_id": chunk["doc_id"], "chunk_id": chunk["_id"],
                                          "chunk_index": chunk["chunk_index"], "dept_id": dept_id}]
        created_at = _iso(source.get("acceptedAt"), now - timedelta(days=5, hours=index))
        success = bool(metrics.get("routing_topk")) and float(metrics.get("asr_cer", 1)) <= .1
        query = "；".join(filter(None, [str(expected.get("event", "")), str(expected.get("request", ""))]))
        trace_id = f"demo_trace_{case_id}"
        await store.insert_trace({
            "_id": trace_id, "session_id": f"demo_session_{case_id}", "user_id": "operator", "query": query,
            "intent": {"type": "complaint", "depts": [dept_id], "category": category},
            "retrieved_chunks": citation, "answer": f"已生成工单并建议转派至{DEPARTMENT_NAMES.get(dept_id, dept_id)}。",
            "citations": citation, "verification": {"passed": success, "source": "synthetic_audio_evaluation"},
            "latency_ms": round(float(case.get("runtime_seconds", 0)) * 1000), "cost": 0.0, "success": success,
            "created_at": created_at, "demo_seed": True, "demo_source": DEMO_TAG,
        })
        counts["traces"] += 1
        await store.insert_feedback({
            "_id": f"demo_feedback_{case_id}", "trace_id": trace_id, "session_id": f"demo_session_{case_id}",
            "user_id": "demo_evaluator", "query": query,
            "answer": f"路由 Top-K={'命中' if metrics.get('routing_topk') else '未命中'}，CER={metrics.get('asr_cer', 0)}",
            "kind": "auto", "signal": "up" if success else "down", "dept_ids": [dept_id], "intent_type": "complaint",
            "detail": {"dept_id": dept_id, "case_id": case_id, "source": "deidentified_synthetic_evaluation"},
            "consumed": index < max(0, len(cases) - 6), "created_at": created_at, "demo_seed": True,
        })
        counts["feedback"] += 1
        await store.upsert("memory_usage", {
            "_id": f"demo_memory_usage_{case_id}", "session_id": f"demo_session_{case_id}",
            "trace_id": trace_id, "memory_id": f"demo_org_memory_{dept_id}",
            "memory_plane": "organization", "usage": "routing_and_grounding",
            "created_at": created_at, "demo_seed": True,
        })
        counts["memory_usage"] += 1
        await store.upsert("conversation_events", {"_id": f"demo_event_{case_id}", "session_id": f"demo_session_{case_id}",
            "user_id": "operator", "seq": 1, "type": "workorder_generated", "content": query, "dept_ids": [dept_id],
            "trace_id": trace_id, "metadata": {"demo_seed": True}, "created_at": created_at})
        await store.upsert("conversation_summaries", {"_id": f"demo_summary_{case_id}", "session_id": f"demo_session_{case_id}",
            "user_id": "operator", "summary": str(source.get("content") or query)[:500],
            "resolved_entities": {"category": category, "dept_id": dept_id}, "unresolved_questions": [],
            "citation_ids": [c["chunk_id"] for c in citation], "updated_at": created_at})
        handoff = ("completed", "completed", "processing", "pending")[index % 4]
        await store.upsert("workorders", {
            "_id": case_id, "case_id": case_id, "title": source.get("title") or (case.get("workorder") or {}).get("title", case_id),
            "summary": source.get("content") or query, "content": source.get("content") or query, "region": expected.get("region", ""),
            "source": {"type": "audio", "raw_text": query, "masked_text": query, "audio_file_name": source.get("audioFile"),
                       "received_at": created_at, "source_channel": "脱敏合成12345双人通话", "formatted_text": "", "citizen_text": query,
                       "dialogue_turns": [], "role_format_mode": (case.get("roles") or {}).get("format_mode", "acoustic")},
            "elements": {"time": created_at[:16].replace("T", " "), "time_basis": "received_at", "location": expected.get("location", ""),
                         "subjects": [], "event": expected.get("event", ""), "request": expected.get("request", ""), "contact_hint": ""},
            "missing_fields": [], "ambiguities": [], "clarification_questions": [],
            "quality": {"completeness": metrics.get("field_completeness", 0), "fidelity": metrics.get("content_support_precision", 0),
                        "clarity": 1.0, "overall": round((float(metrics.get("field_completeness", 0)) + float(metrics.get("content_support_precision", 0))) / 2, 4),
                        "warnings": ["演示/脱敏数据"]},
            "generation": {"mode": (case.get("workorder") or {}).get("generation_mode", "rule_fallback"),
                           "provider": "evaluation_replay", "model": "", "fallback_reason": "", "evidence": {}},
            "multiple_matters": False, "matter_candidates": [], "status": "confirmed", "requires_human_review": True,
            "created_at": created_at, "confirmed_at": created_at, "confirmed_by": "operator", "handoff_status": handoff,
            "handed_off_at": created_at, "claimed_at": created_at if handoff != "pending" else None,
            "claimed_by": "cgj_admin" if handoff != "pending" else None,
            "completed_at": created_at if handoff == "completed" else None,
            "classification": {"category": category, "source": "evaluation_ground_truth"},
            "routing": {"dept_ids": [dept_id], "dept_names": [DEPARTMENT_NAMES.get(dept_id, dept_id)], "matched_by": "evaluation_replay"},
            "reply": {"content": source.get("reply", ""), "status": "draft"}, "demo_seed": True, "demo_source": DEMO_TAG,
        })
        counts["workorders"] += 1
    for category, dept_id in CATEGORY_TO_DEPT.items():
        for topic_index, topic in enumerate(issues_by_category.get(category, [])[:3]):
            await store.upsert("memory_topics", {"_id": _stable_id("demo_topic", f"{dept_id}:{topic}"), "dept_id": dept_id,
                "topic_key": topic, "count": 7 - topic_index, "updated_at": now.isoformat(timespec="seconds"), "demo_seed": True})
        dept_chunks = chunks_by_dept.get(dept_id, [])
        await store.upsert("org_memory_items", {"_id": f"demo_org_memory_{dept_id}", "scope": "department", "dept_id": dept_id,
            "type": "knowledge_status", "title": f"{category}知识库运行摘要",
            "content": f"已加载{len(dept_chunks)}个官方文档切片，来源可追溯。", "source_refs": [],
            "source_doc_ids": list(dict.fromkeys(c["doc_id"] for c in dept_chunks[:5])), "authority": "official_document",
            "confidence": 1.0, "review_status": "approved", "status": "active",
            "access_scope": ["operator", "department_admin", "system_admin"], "revision": 1, "demo_seed": True})
        counts["organization_memory"] += 1
    return dict(counts)


async def _seed_evolution(store, evaluation: dict[str, Any]) -> None:
    summary = evaluation.get("summary") or {}
    cases = evaluation.get("cases") or []
    now = datetime.now(CHINA_TZ)
    total = len(cases)
    routing_hits = sum(1 for case in cases if (case.get("metrics") or {}).get("routing_topk"))
    role_hits = sum(1 for case in cases if (case.get("metrics") or {}).get("role_sequence_exact"))
    emergency_hits = sum(
        1 for case in cases
        if any(word in json.dumps(case, ensure_ascii=False) for word in ("燃气", "火灾", "危险", "人身安全", "污染"))
    )
    metric_updates = {
        "skill_work_order_intake_quality_seed": (total, role_hits, .083),
        "skill_policy_grounded_reply_seed": (total, routing_hits, .056),
        "skill_emergency_dispatch_seed": (emergency_hits, emergency_hits, .0),
    }
    for skill_id, (trigger_count, success_count, replay_delta) in metric_updates.items():
        skill = await store.get_skill(skill_id)
        if not skill:
            continue
        skill["metrics"] = {
            "trigger_count": trigger_count, "success_count": success_count,
            "success_rate": round(success_count / max(trigger_count, 1), 4) if trigger_count else 0.0,
            "avg_latency_ms": round(float(summary.get("runtime_minutes", 0)) * 60_000 / max(total, 1)),
            "last_triggered": now.isoformat(timespec="seconds"),
        }
        skill["replay"] = {"cases": trigger_count, "delta": replay_delta,
                           "source": "36条脱敏合成双人通话评测"}
        skill["demo_seed"] = True
        await store.upsert_skill(skill)
    for index, _dept_id in enumerate(CATEGORY_TO_DEPT.values()):
        await store.upsert("strategy_executions", {"_id": f"demo_execution_{index:02d}",
            "artifact_id": "skill_work_order_intake_quality_seed", "artifact_type": "skill", "version": 1,
            "group": "treatment" if index < 8 else "control", "success": index not in (3, 8),
            "latency_ms": 820 + index * 37, "created_at": (now - timedelta(days=2, hours=index)).isoformat(timespec="seconds"), "demo_seed": True})
    await store.upsert("experiments", {"_id": "demo_experiment_synthetic_audio_v1",
        "artifact_id": "skill_work_order_intake_quality_seed", "name": "合成双人通话工单链路灰度回放", "status": "running",
        "treatment_percent": .67, "metrics": {"cases": summary.get("successful_cases", 0),
            "asr_cer": (summary.get("asr") or {}).get("cer_mean"), "routing_topk": (summary.get("routing") or {}).get("topk_accuracy")},
        "created_at": (now - timedelta(days=3)).isoformat(timespec="seconds"), "demo_seed": True})
    await store.upsert("strategy_proposals", {"_id": "demo_proposal_public_service_routing",
        "skill_ids": ["skill_work_order_intake_quality_seed"], "title": "补强公共服务事项的部门路由词表", "status": "pending",
        "reason": "脱敏合成评测中公共服务 Top-K 命中率偏低", "created_at": (now - timedelta(days=1)).isoformat(timespec="seconds"), "demo_seed": True})
    for candidate_id, title, reason in (
        ("route_public_service", "公共服务路由词表候选", "公共服务类 Top-K 路由存在未命中样本"),
        ("structured_output_guard", "结构化工单输出修复候选", "部分样本触发大模型 JSON 解析降级"),
    ):
        await store.upsert("memory_candidates", {
            "_id": f"demo_candidate_{candidate_id}", "type": "learning_candidate", "title": title,
            "content": reason, "status": "pending", "evidence_count": total,
            "source": "deidentified_synthetic_evaluation", "created_at": now.isoformat(timespec="seconds"),
            "demo_seed": True,
        })
    feedback = await store.find("feedback")
    signals = {key: sum(1 for row in feedback if row.get("signal") == key) for key in ("up", "down", "correction")}
    result = {"summary": "完成36条脱敏合成通话回放，识别出路由与字段抽取改进项",
        "next_action": "优先补强公共服务路由词表，并提高大模型结构化输出稳定性。",
        "duration_ms": round(float(summary.get("runtime_minutes", 0)) * 60 * 1000), "observed": len(feedback),
        "bad_cases": sum(1 for c in evaluation.get("cases", []) if not (c.get("metrics") or {}).get("routing_topk")),
        "signals": signals, "reflect": {"root_causes": {"route_miss": 9, "field_extraction": 7, "llm_json_fallback": 14}},
        "adaptations": [{"id": "demo_proposal_public_service_routing", "type": "rule", "name": "公共服务路由增强候选", "auto_activated": False}],
        "deployed": {"skills": 0, "hooks": 0, "rules": 0}, "changes": {"skills": 0, "hooks": 0, "rules": 1}}
    await store.upsert("async_jobs", {"_id": "demo_loop_job_20260829", "type": "run_loop", "status": "completed",
        "progress": {"stage": "complete", "detail": "脱敏评测回放与根因聚合完成"}, "result": result,
        "created_at": (now - timedelta(days=1, hours=2)).isoformat(timespec="seconds"),
        "updated_at": (now - timedelta(days=1)).isoformat(timespec="seconds"), "demo_seed": True})


async def seed_operational_demo_data(store) -> dict[str, int]:
    """幂等加载；pytest 和资料缺失时快速跳过。"""
    if os.getenv("PYTEST_CURRENT_TEST") or not KNOWLEDGE_MANIFEST.is_file():
        return {}
    rows = _load_knowledge_manifest()
    if not rows:
        return {}
    evaluation = _load_json(EVALUATION_REPORT, {})
    synthetic_rows = _load_json(SYNTHETIC_MANIFEST, [])
    docs_by_category, chunks_by_dept = await _seed_documents(store, rows)
    review_orders = await _seed_reviews(store, docs_by_category, evaluation)
    runtime = await _seed_runtime(store, evaluation, synthetic_rows, chunks_by_dept)
    await _seed_evolution(store, evaluation)
    return {"documents": sum(map(len, docs_by_category.values())), "chunks": sum(map(len, chunks_by_dept.values())),
            "review_orders": review_orders, **runtime}
