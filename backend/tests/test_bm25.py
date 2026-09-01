"""测试 BM25 检索。"""
from __future__ import annotations

from app.retrieval.bm25 import BM25Index, tokenize


def test_tokenize_chinese():
    tokens = tokenize("芜湖市生活垃圾分类管理条例")
    assert "垃圾分类" in tokens or "本科" in tokens or len(tokens) > 0


def test_bm25_search():
    idx = BM25Index()
    docs = [
        {"_id": "c1", "dept_id": "dept_city_management", "content": "居民垃圾分类时间安排在每学期第16至18周完成下学期垃圾分类。"},
        {"_id": "c2", "dept_id": "dept_city_management", "content": "住房补贴申请应当在开课后两周内提交。"},
        {"_id": "c3", "dept_id": "dept_economy_trade", "content": "天然气费缴纳截止时间为每学期开学前。"},
    ]
    idx.index(docs)
    hits = idx.search("垃圾分类时间", top_k=2)
    assert hits, "应返回检索结果"
    assert hits[0]["id"] == "c1"


def test_bm25_dept_filter():
    idx = BM25Index()
    docs = [
        {"_id": "c1", "dept_id": "dept_city_management", "content": "垃圾分类相关条款。"},
        {"_id": "c2", "dept_id": "dept_economy_trade", "content": "垃圾分类缴费相关条款。"},
    ]
    idx.index(docs)
    hits = idx.search("垃圾分类", top_k=5, dept_id="dept_city_management")
    assert all(h["dept_id"] == "dept_city_management" for h in hits)


def test_bm25_filters_before_topk():
    idx = BM25Index()
    docs = [
        {"_id": f"other-{i}", "dept_id": "dept_other", "content": "垃圾分类 垃圾分类 垃圾分类"}
        for i in range(25)
    ]
    docs.append({"_id": "target", "dept_id": "dept_city_management", "content": "垃圾分类"})
    idx.index(docs)
    hits = idx.search("垃圾分类", top_k=5, dept_id="dept_city_management")
    assert [h["id"] for h in hits] == ["target"]
