"""在芜湖 12345 官方政务文档集上评估混合检索与答案质量。

指标：Recall@5、MRR、引用正确率、答案一致性（标准答案关键点覆盖率）。
默认评估已入库的芜湖官方政务文档。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.deps import build_container
from scripts.ingest_department_files import resolve_dept

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evaluation" / "real_document_qa.json"


def _normalize(text: str) -> str:
    return "".join(text.lower().split()).replace("：", ":").replace("－", "-").replace("‑", "-")


def _source_file(hit: dict[str, Any], docs: dict[str, dict[str, Any]]) -> str:
    return str((docs.get(hit.get("doc_id", ""), {}).get("source") or {}).get("file_name", ""))


async def evaluate(dataset_path: Path, top_k: int = 5, ingest_base: Path | None = None) -> dict[str, Any]:
    settings = get_settings()
    container = build_container(settings)
    if container.mongo is not None:
        await container.mongo.connect()
    if hasattr(container.session_store, "connect"):
        try:
            await container.session_store.connect()
        except Exception:
            pass

    if ingest_base:
        suffixes = {".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm"}
        for path in sorted(p for p in ingest_base.rglob("*") if p.is_file() and p.suffix.lower() in suffixes):
            await container.indexer.ingest(path, resolve_dept(path), uploaded_by="evaluation")

    active_chunks = await container.store.list_active_chunks()
    if not active_chunks:
        raise RuntimeError("没有可评测的 active chunks；请先入库文档或传入 --ingest-base")
    container.bm25.index(active_chunks)
    if settings.vector_backend != "mongo" or await container.vector_store.count() == 0:
        vectors = await container.embeddings.embed([c.get("retrieval_text") or c["content"] for c in active_chunks])
        for chunk, vector in zip(active_chunks, vectors):
            await container.vector_store.add(
                chunk["_id"], vector,
                {"doc_id": chunk["doc_id"], "dept_id": chunk["dept_id"], "chunk_index": chunk["chunk_index"]},
            )

    cases = json.loads(dataset_path.read_text(encoding="utf-8"))
    docs = {d["_id"]: d for d in await container.store.list_documents(status="active")}
    rows: list[dict[str, Any]] = []
    reciprocal_ranks: list[float] = []
    citation_scores: list[float] = []
    consistency_scores: list[float] = []
    answer_case_count = 0

    for case in cases:
        hits = await container.retrieval_agent.retrieve([case["query"]], [case["dept_id"]], top_k=top_k)
        relevant_files = set(case.get("relevant_files") or [])
        relevant_titles = set(case.get("relevant_titles") or [])
        rank = next((i for i, hit in enumerate(hits, 1) if _source_file(hit, docs) in relevant_files or docs.get(hit.get("doc_id", ""), {}).get("title") in relevant_titles), 0)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)

        cited = []
        answer_content = ""
        if not case.get("retrieval_only", False):
            answer_case_count += 1
            rules = await container.rule_engine.active_rules()
            answer = await container.orchestrator.answer_agent.generate(case["query"], hits, rules=rules)
            cited = answer.citations
            answer_content = answer.content
        valid_citations = 0
        for citation in cited:
            chunk = await container.store.get("chunks", f"{citation.doc_id}:{citation.chunk_index}")
            doc = docs.get(citation.doc_id)
            if chunk and doc and doc.get("status") == "active" and citation.snippet in chunk.get("content", ""):
                valid_citations += 1
        citation_score = valid_citations / len(cited) if cited else 0.0
        if not case.get("retrieval_only", False):
            citation_scores.append(citation_score)

        normalized_answer = _normalize(answer_content)
        terms = case.get("expected_terms") or []
        covered = sum(1 for term in terms if _normalize(term) in normalized_answer)
        consistency = covered / len(terms) if terms and answer_content else (1.0 if not terms else 0.0)
        if not case.get("retrieval_only", False):
            consistency_scores.append(consistency)
        rows.append({
            "id": case["id"], "rank": rank, "hit_at_5": bool(rank and rank <= 5),
            "citation_correctness": round(citation_score, 4) if not case.get("retrieval_only", False) else None,
            "answer_consistency": round(consistency, 4) if not case.get("retrieval_only", False) else None,
            "retrieved_titles": [docs.get(h.get("doc_id", ""), {}).get("title", "") for h in hits],
            "retrieval_branches": [h.get("retrieval_branches", []) for h in hits],
        })

    n = max(len(cases), 1)
    report = {
        "dataset": str(dataset_path), "cases": len(cases), "top_k": top_k,
        "recall_at_5": round(sum(1 for r in rows if r["hit_at_5"]) / n, 4),
        "mrr": round(sum(reciprocal_ranks) / n, 4),
        "citation_correctness": round(sum(citation_scores) / answer_case_count, 4) if answer_case_count else None,
        "answer_consistency": round(sum(consistency_scores) / answer_case_count, 4) if answer_case_count else None,
        "details": rows,
    }
    if container.mongo is not None:
        await container.mongo.close()
    return report


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ingest-base", type=Path, help="评测前在同一进程导入该目录（适合 memory 模式）")
    args = parser.parse_args()
    report = await evaluate(args.dataset, args.top_k, args.ingest_base)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    asyncio.run(main())
