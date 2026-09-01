"""审计芜湖政务 RAG 的持久化文档、chunk、向量和旧场景残留。"""
from __future__ import annotations

import asyncio
import json
from collections import Counter

from app.config import get_settings
from app.deps import build_container
from app.domain.wuhu import DEPARTMENTS, LEGACY_DEPARTMENT_IDS


async def audit() -> dict:
    settings = get_settings()
    container = build_container(settings)
    if container.mongo is not None:
        await container.mongo.connect()
    try:
        store = container.store
        departments = await store.find("departments")
        documents = await store.list_documents(status="active")
        chunks = await store.list_active_chunks()
        vectors = await store.find("vector_embeddings")
        doc_ids = {row["_id"] for row in documents}
        chunk_ids = {row["_id"] for row in chunks}
        expected_depts = {row["_id"] for row in DEPARTMENTS}
        vector_dims = sorted({len(row.get("vector") or []) for row in vectors})
        return {
            "departments": len(departments),
            "expected_departments_present": expected_depts <= {row["_id"] for row in departments},
            "legacy_departments": [row["_id"] for row in departments if row["_id"] in LEGACY_DEPARTMENT_IDS],
            "active_documents": len(documents),
            "active_chunks": len(chunks),
            "vectors": len(vectors),
            "vector_dimensions": vector_dims,
            "documents_by_category": dict(Counter(row.get("category") or "未分类" for row in documents)),
            "documents_with_legacy_dept": sum(row.get("dept_id") in LEGACY_DEPARTMENT_IDS for row in documents),
            "chunks_missing_retrieval_text": sum(not row.get("retrieval_text") for row in chunks),
            "chunks_missing_source_url": sum(not (row.get("metadata") or {}).get("source_url") for row in chunks),
            "orphan_chunks": sum(row.get("doc_id") not in doc_ids for row in chunks),
            "orphan_vectors": sum(row.get("_id") not in chunk_ids for row in vectors),
        }
    finally:
        if container.mongo is not None:
            await container.mongo.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(audit()), ensure_ascii=False, indent=2))
