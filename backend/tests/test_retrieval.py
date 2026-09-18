"""测试混合检索（内存向量 + BM25 + 启发式重排）。"""
from __future__ import annotations

import pytest

from app.retrieval.bm25 import BM25Index
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.reranker import HeuristicReranker
from app.retrieval.vector_store import MemoryVectorStore
from app.storage.store import MemoryStore


@pytest.mark.asyncio
async def test_hybrid_retrieve(embeddings):
    bm25 = BM25Index()
    vs = MemoryVectorStore()
    docs = [
        {"_id": "c1", "doc_id": "d1", "dept_id": "dept_city_management", "chunk_index": 0, "content": "芜湖市生活垃圾分类管理条例规定垃圾分类时间。"},
        {"_id": "c2", "doc_id": "d2", "dept_id": "dept_city_management", "chunk_index": 0, "content": "住房补贴应当在开课后两周内申请。"},
        {"_id": "c3", "doc_id": "d3", "dept_id": "dept_economy_trade", "chunk_index": 0, "content": "天然气费缴纳方式与时间安排。"},
    ]
    bm25.index(docs)
    vecs = await embeddings.embed([d["content"] for d in docs])
    for d, v in zip(docs, vecs):
        await vs.add(d["_id"], v, {"doc_id": d["doc_id"], "dept_id": d["dept_id"]})

    hybrid = HybridRetriever(bm25=bm25, vector_store=vs, reranker=HeuristicReranker(), top_k=3)
    query_vec = await embeddings.embed_query("垃圾分类时间是什么时候")
    hits = await hybrid.retrieve("垃圾分类时间是什么时候", query_vec)
    assert hits, "应返回检索结果"
    assert hits[0]["id"] in {"c1", "c2", "c3"}


@pytest.mark.asyncio
async def test_vector_store_cosine(embeddings):
    vs = MemoryVectorStore()
    v = await embeddings.embed(["芜湖市生活垃圾分类管理条例"])
    await vs.add("a", v[0], {"dept_id": "dept_city_management"})
    hits = await vs.search(v[0], top_k=1)
    assert hits and hits[0]["id"] == "a"
    assert hits[0]["score"] > 0.9


@pytest.mark.asyncio
async def test_retrieval_hydrates_vector_only_hit_and_filters_archived(embeddings):
    from app.harness.agents.retrieval_agent import RetrievalAgent

    store = MemoryStore()
    bm25 = BM25Index()
    vs = MemoryVectorStore()
    hybrid = HybridRetriever(bm25=bm25, vector_store=vs, reranker=HeuristicReranker(), top_k=5)
    agent = RetrievalAgent(hybrid, embeddings, store)
    for doc_id, status in (("active-doc", "active"), ("old-doc", "archived")):
        await store.insert_document({"_id": doc_id, "dept_id": "dept_city_management", "title": doc_id, "status": status})
        chunk = {
            "_id": f"{doc_id}:0", "doc_id": doc_id, "dept_id": "dept_city_management",
            "chunk_index": 0, "content": "农村宅基地管理办法不少于五千字", "keywords": ["宅基地"],
            "section_path": [], "section_title": "要求",
        }
        await store.insert_chunks([chunk])
        vector = await embeddings.embed_query(chunk["content"])
        await vs.add(chunk["_id"], vector, {"doc_id": doc_id, "dept_id": "dept_city_management", "chunk_index": 0})
    hits = await agent.retrieve(["农村宅基地管理办法不少于五千字"], ["dept_city_management"], top_k=5)
    assert len(hits) == 1
    assert hits[0]["doc_id"] == "active-doc"
    assert hits[0]["content"]


@pytest.mark.asyncio
async def test_multi_query_retrieval_rewards_chunks_confirmed_by_multiple_queries(embeddings):
    from app.harness.agents.retrieval_agent import RetrievalAgent

    class FakeHybrid:
        async def retrieve(self, query, query_vec, dept_id=None):
            if query == "原问题":
                return [{"id": "c1", "_rrf": 0.5}, {"id": "c2", "_rrf": 0.8}]
            return [{"id": "c1", "_rrf": 0.5}]

    store = MemoryStore()
    await store.insert_document({"_id": "d1", "dept_id": "dept_city_management", "title": "条例", "status": "active"})
    await store.insert_chunks([
        {"_id": "c1", "doc_id": "d1", "dept_id": "dept_city_management", "chunk_index": 0, "content": "共同命中"},
        {"_id": "c2", "doc_id": "d1", "dept_id": "dept_city_management", "chunk_index": 1, "content": "单次命中"},
    ])
    agent = RetrievalAgent(FakeHybrid(), embeddings, store)

    hits = await agent.retrieve(["原问题", "扩展问题"], ["dept_city_management"], top_k=2)

    assert hits[0]["id"] == "c1"
    assert hits[0]["matched_queries"] == ["原问题", "扩展问题"]


@pytest.mark.asyncio
async def test_multi_query_retrieval_reserves_one_hit_per_subquestion(embeddings):
    from app.harness.agents.retrieval_agent import RetrievalAgent

    class FakeHybrid:
        async def retrieve(self, query, query_vec, dept_id=None):
            if query == "主题一":
                return [{"id": "c1", "_rrf": 0.9}, {"id": "c2", "_rrf": 0.8}]
            return [{"id": "c3", "_rrf": 0.3}, {"id": "c1", "_rrf": 0.9}]

    store = MemoryStore()
    await store.insert_document({"_id": "d1", "dept_id": "dept_health", "title": "政策", "status": "active"})
    await store.insert_chunks([
        {"_id": key, "doc_id": "d1", "dept_id": "dept_health", "chunk_index": index, "content": key}
        for index, key in enumerate(("c1", "c2", "c3"))
    ])
    hits = await RetrievalAgent(FakeHybrid(), embeddings, store).retrieve(
        ["主题一", "主题二"], ["dept_health"], top_k=2
    )

    assert [item["id"] for item in hits] == ["c1", "c3"]


@pytest.mark.asyncio
async def test_multi_query_retrieval_rewards_chunks_confirmed_by_multiple_queries(embeddings):
    from app.harness.agents.retrieval_agent import RetrievalAgent

    class FakeHybrid:
        async def retrieve(self, query, query_vec, dept_id=None):
            if query == "原问题":
                return [{"id": "c1", "_rrf": 0.5}, {"id": "c2", "_rrf": 0.8}]
            return [{"id": "c1", "_rrf": 0.5}]

    store = MemoryStore()
    await store.insert_document({"_id": "d1", "dept_id": "dept_city_management", "title": "条例", "status": "active"})
    await store.insert_chunks([
        {"_id": "c1", "doc_id": "d1", "dept_id": "dept_city_management", "chunk_index": 0, "content": "共同命中"},
        {"_id": "c2", "doc_id": "d1", "dept_id": "dept_city_management", "chunk_index": 1, "content": "单次命中"},
    ])
    agent = RetrievalAgent(FakeHybrid(), embeddings, store)

    hits = await agent.retrieve(["原问题", "扩展问题"], ["dept_city_management"], top_k=2)

    assert hits[0]["id"] == "c1"
    assert hits[0]["matched_queries"] == ["原问题", "扩展问题"]
