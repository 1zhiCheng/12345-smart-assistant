"""测试 Agent 的无 LLM 回退逻辑。"""
from __future__ import annotations

from app.harness.base import Answer, VerificationResult
from app.harness.agents.intent_agent import IntentAgent
from app.harness.agents.answer_agent import AnswerAgent
from app.harness.agents.answer_agent import AnswerAgent
from app.harness.agents.query_rewriter import QueryRewriter
from app.harness.agents.verifier_agent import VerifierAgent


class _FakeLLM:
    """模拟 LLM，complete 抛错以测试回退。"""

    def __init__(self):
        self.calls = 0

    async def complete(self, *a, **kw):
        self.calls += 1
        raise RuntimeError("no llm")

    async def complete_json(self, *a, **kw):
        self.calls += 1
        raise RuntimeError("no llm")


class _EmptyLLM(_FakeLLM):
    async def complete(self, *a, **kw):
        self.calls += 1
        return "   "


class _RewriteLLM(_FakeLLM):
    async def complete_json(self, *a, **kw):
        return {"queries": ["生活垃圾分类处置"]}


class _AnswerLLM(_FakeLLM):
    def __init__(self):
        super().__init__()
        self.kwargs = {}

    async def complete(self, *a, **kw):
        self.kwargs = kw
        return "生活垃圾应当分类投放。[来源1]"


def test_intent_fallback():
    store = _MemStore()
    agent = IntentAgent(_FakeLLM(), store)
    import asyncio

    intent = asyncio.run(agent.infer("生活垃圾分类规定什么时候生效？", "u1", "上一轮事项：垃圾分类"))
    assert intent.type == "deadline_query"
    assert "dept_city_management" in intent.depts


def test_query_rewriter_fallback():
    store = _MemStore()
    rw = QueryRewriter(_FakeLLM(), store)
    import asyncio

    queries = asyncio.run(rw.rewrite("住房补贴怎么办", None))
    assert queries and queries[0] == "住房补贴怎么办"
    assert any("办理流程" in query for query in queries)


def test_successful_model_rewrite_keeps_original_and_deterministic_expansion():
    import asyncio

    queries = asyncio.run(QueryRewriter(_RewriteLLM(), _MemStore()).rewrite(
        "小区不按规定分类投放生活垃圾应如何处理？"
    ))

    assert queries[0] == "小区不按规定分类投放生活垃圾应如何处理？"
    assert "生活垃圾分类处置" in queries
    assert any("责令改正" in query and "处罚" in query for query in queries)


def test_query_rewriter_splits_compound_policy_question():
    import asyncio

    queries = asyncio.run(QueryRewriter(_FakeLLM(), _MemStore()).rewrite(
        "一对夫妻可以生育几个子女，国家基础育儿补贴标准是多少？"
    ))

    assert "一对夫妻可以生育几个子女" in queries
    assert "国家基础育儿补贴标准是多少" in queries


def test_query_rewriter_expands_common_government_question_types():
    store = _MemStore()
    rw = QueryRewriter(_FakeLLM(), store)
    import asyncio

    material_queries = asyncio.run(rw.rewrite("办理电子普通护照需要哪些材料？", None))
    enforcement_queries = asyncio.run(rw.rewrite("不按规定分类投放生活垃圾应如何处理？", None))

    assert any("申请材料" in query for query in material_queries)
    assert any("责令改正" in query for query in enforcement_queries)
    assert any("办理流程" in query for query in queries)


def test_query_rewriter_expands_common_government_question_types():
    store = _MemStore()
    rw = QueryRewriter(_FakeLLM(), store)
    import asyncio

    material_queries = asyncio.run(rw.rewrite("办理电子普通护照需要哪些材料？", None))
    enforcement_queries = asyncio.run(rw.rewrite("不按规定分类投放生活垃圾应如何处理？", None))

    assert any("申请材料" in query for query in material_queries)
    assert any("责令改正" in query for query in enforcement_queries)


def test_verifier_heuristic():
    v = VerifierAgent(_FakeLLM())
    import asyncio

    answer = Answer(content="根据规定应于第8周前住房补贴", citations=[])
    result = asyncio.run(v.verify("住房补贴时间", answer, []))
    assert isinstance(result, VerificationResult)


def test_answer_empty_llm_output_uses_grounded_fallback():
    import asyncio

    chunk = {
        "doc_id": "doc-1", "dept_id": "dept-city", "chunk_index": 0,
        "content": "生活垃圾应当分类投放。", "section_path": [],
    }
    answer = asyncio.run(AnswerAgent(_EmptyLLM(), _MemStore()).generate("垃圾如何投放", [chunk]))

    assert answer.content.startswith("根据检索到的制度条款")
    assert "生活垃圾应当分类投放" in answer.content
    assert answer.generation_mode == "official_text_fallback"


def test_answer_generation_disables_long_thinking():
    import asyncio

    llm = _AnswerLLM()
    answer = asyncio.run(AnswerAgent(llm, _MemStore()).generate(
        "垃圾如何投放", [{
            "doc_id": "doc-1", "dept_id": "dept-city", "chunk_index": 0,
            "content": "生活垃圾应当分类投放。", "section_path": [],
        }]
    ))

    assert answer.generation_mode == "llm"
    assert llm.kwargs["thinking"] == {"type": "disabled"}


def test_verifier_rejects_contradictory_pass_with_issues():
    import asyncio

    class ContradictoryLLM(_FakeLLM):
        async def complete_json(self, *a, **kw):
            return {"passed": True, "score": 0.9, "issues": ["引用来源标注有误"]}

    answer = Answer(content="应当提交材料。[来源1]", citations=[object()])
    result = asyncio.run(VerifierAgent(ContradictoryLLM()).verify(
        "如何办理", answer, [{"content": "应当提交材料。"}]
    ))

    assert result.passed is False
    assert result.issues == ["引用来源标注有误"]


def test_verifier_ignores_explanations_that_explicitly_say_no_error():
    import asyncio

    class ExplanatoryLLM(_FakeLLM):
        async def complete_json(self, *a, **kw):
            return {"passed": True, "score": 0.95, "issues": ["答案已如实说明，不构成错误"]}

    answer = Answer(content="应当提交材料。[来源1]", citations=[object()])
    result = asyncio.run(VerifierAgent(ExplanatoryLLM()).verify(
        "如何办理", answer, [{"content": "应当提交材料。"}]
    ))

    assert result.passed is True
    assert result.issues == []


def test_answer_fallback_extracts_relevant_clause_beyond_first_300_chars():
    noise = "本段为政策背景说明，与具体处理措施无关。" * 25
    chunks = [{
        "content": noise + "未在指定地点分类投放生活垃圾的，由城市管理部门责令改正；情节严重的依法处罚。"
    }]

    answer = AnswerAgent._fallback_answer("不按规定分类投放生活垃圾应如何处理？", chunks)

    assert "责令改正" in answer
    assert "依法处罚" in answer


def test_answer_fallback_keeps_each_source_in_one_citable_paragraph():
    answer = AnswerAgent._fallback_answer(
        "如何办理", [{"content": "申请人应当提交材料。\n办理期限以公告为准。"}]
    )

    assert answer.splitlines() == [
        "根据检索到的制度条款：", "[来源1] 申请人应当提交材料。 办理期限以公告为准。"
    ]


def test_answer_fallback_extracts_relevant_clause_beyond_first_300_chars():
    noise = "本段为政策背景说明，与具体处理措施无关。" * 25
    chunks = [{
        "content": noise + "未在指定地点分类投放生活垃圾的，由城市管理部门责令改正；情节严重的依法处罚。"
    }]

    answer = AnswerAgent._fallback_answer("不按规定分类投放生活垃圾应如何处理？", chunks)

    assert "责令改正" in answer
    assert "依法处罚" in answer


class _MemStore:
    async def list_departments(self):
        return [{"_id": "dept_city_management", "name": "城市管理局"}, {"_id": "dept_economy_trade", "name": "发展改革委"}]

    async def get_user_profile(self, user_id):
        return None

    async def list_glossary(self):
        return []
