"""成员 A：诉求理解与标准工单生成测试。"""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.intake.llm_service import LLMIntakeService
from app.intake.service import IntakeService
from app.main import app


class FakeLLM:
    api_key = "test-key"
    model = "test-model"

    def __init__(self, payload):
        self.payload = payload
        self.messages = []

    async def complete_json(self, messages, **kwargs):
        self.messages = messages
        return self.payload

    def name(self):
        return "fake"


def test_analyze_complete_appeal():
    service = IntakeService()
    result = service.analyze(
        "市民反映昨晚23点镜湖区东方小区二期施工单位持续施工，噪声很大，希望立即停止夜间施工并调查处理。"
    )

    assert result.case_id.startswith("WH-")
    assert "昨晚23点" in result.elements.time
    assert result.elements.location == "镜湖区东方小区二期"
    assert result.title.startswith("关于镜湖区东方小区二期")
    assert result.elements.request.startswith("希望")
    assert result.elements.event
    assert result.elements.request
    assert result.quality.completeness == 1.0
    assert result.requires_human_review is True


def test_audio_workorder_uses_concise_fact_summary():
    raw = (
        "经济技术开发区衡山支路靠近安徽美芝精密制造有限公司，有商贩摆摊。"
        "绿化围栏和路灯被拆坏，晚上还有私家车非法营运。"
    )
    result = IntakeService().analyze(raw, source_type="audio")
    assert result.elements.location == "经济技术开发区衡山支路靠近安徽美芝精密制造有限公司"
    assert "商贩" in result.elements.event
    assert "绿化围栏" in result.elements.event
    assert "路灯" in result.elements.event
    assert "非法营运" in result.elements.event
    assert len(result.content) < 180


def test_multiple_matters_can_be_split_into_reviewable_drafts():
    result = IntakeService().analyze(
        "镜湖区某小区门口有商贩摆摊，同时路灯被损坏，还有私家车非法营运，希望核查处理。"
    )
    assert result.multiple_matters is True
    assert {item.topic for item in result.matter_candidates} >= {"占道经营", "市政设施", "交通运输"}

    drafts = IntakeService().split_workorder(result, [item.id for item in result.matter_candidates])
    assert len(drafts) >= 3
    assert all(item.parent_case_id == result.case_id for item in drafts)
    assert all(item.status == "draft" and item.requires_human_review for item in drafts)
    assert len({item.case_id for item in drafts}) == len(drafts)


def test_missing_information_generates_questions():
    result = IntakeService().analyze("这里一直很吵，希望相关部门处理。")

    assert "time" not in result.missing_fields
    assert result.elements.time_basis == "received_at"
    assert "按来电时间" in result.elements.time
    assert "location" in result.missing_fields
    assert result.clarification_questions
    assert "存在指代不明确" in result.ambiguities


def test_missing_event_time_uses_received_at_in_wuhu_timezone():
    result = IntakeService().analyze(
        "镜湖区某小区存在施工噪声，希望处理。",
        received_at="2026-08-27T14:35:00+08:00",
    )
    assert result.source.received_at == "2026-08-27T14:35:00+08:00"
    assert result.elements.time == "2026年8月27日 14:35（按来电时间）"
    assert result.elements.time_basis == "received_at"


def test_sensitive_information_is_masked():
    result = IntakeService().analyze("我的电话是13800138000，身份证340202199001011234，请联系我处理。")

    assert "13800138000" not in result.source.masked_text
    assert "340202199001011234" not in result.source.masked_text
    assert "[联系电话已脱敏]" in result.source.masked_text
    assert "[身份证号已脱敏]" in result.source.masked_text


def test_audio_transcript_is_cleaned_but_raw_text_is_preserved():
    raw = (
        "您好，我是12345热线，请问有什么可以帮助您？嗯，对对对。"
        "杨先生反映经济技术开发区衡山支路有商贩摆摊，绿化围栏和路灯被破坏。"
        "晚上还有私家车非法营运，希望相关部门管理。感谢您的来电。"
    )
    result = IntakeService().analyze(raw, source_type="audio", audio_file_name="case.mp3")
    assert result.source.raw_text == raw
    assert "请问有什么可以帮助您" in result.source.masked_text
    assert "请问有什么可以帮助您" not in result.content
    assert "商贩" in result.elements.event
    assert "摆摊" in result.elements.event
    assert result.elements.location.startswith("经济技术开发区衡山支路")
    assert result.elements.request.startswith("希望")


def test_audio_analysis_accepts_separate_raw_transcript_for_audit():
    raw = "您好，我是12345热线。群众口述原始录音内容。"
    edited = "群众反映：经济技术开发区衡山支路有商贩摆摊。诉求：希望相关部门管理。"
    result = IntakeService().analyze(edited, source_type="audio", raw_transcript=raw)
    assert result.source.raw_text == raw
    assert "12345热线" not in result.content
    assert "商贩" in result.elements.event


def test_audio_dialogue_is_formatted_and_only_citizen_turns_feed_workorder():
    raw = (
        "您好，这里是芜湖12345热线，请问有什么可以帮您。"
        "我想反映我们小区西门垃圾投放点每天早上六点半冲地，噪声很大。"
        "请问具体在什么位置，您希望怎么处理。"
        "镜湖区某小区西门，希望八点以后再冲地。"
        "好的，我复述一下，您反映的是环卫噪声问题，我已经记录，会转交镜湖区政府。"
    )
    service = IntakeService()
    dialogue = service.format_audio_dialogue(raw)
    assert dialogue.mode == "heuristic"
    assert any(turn.role == "operator" for turn in dialogue.turns)
    assert any(turn.role == "citizen" for turn in dialogue.turns)
    assert "接线员：" in dialogue.formatted_text
    assert "群众：" in dialogue.formatted_text
    assert "会转交镜湖区政府" not in dialogue.citizen_text

    result = service.analyze(dialogue.formatted_text, source_type="audio", raw_transcript=raw)
    assert result.source.raw_text == raw
    assert result.source.role_format_mode == "labeled"
    assert "请问具体在什么位置" not in result.elements.request
    assert "会转交镜湖区政府" not in result.elements.request
    assert result.elements.location.startswith("镜湖区")


def test_audio_monologue_is_not_forced_into_fake_roles():
    dialogue = IntakeService.format_audio_dialogue("镜湖区某小区夜间施工噪声很大，希望相关部门处理。")
    assert dialogue.mode == "unsegmented"
    assert len(dialogue.turns) == 1
    assert dialogue.turns[0].role == "citizen"


def test_dialogue_formatter_splits_role_change_when_asr_misses_punctuation():
    raw = (
        "您好，芜湖12345热线，请讲一下您要反映的问题。"
        "你好，我讲一下，我们这边商业街晚上摆摊太多，人只能绕到马路上 您说的是哪个区，每天几点最集中。"
        "鸠江区这边，晚上七点以后最多。"
        "好的，我复述一下，您反映的是流动摊贩占道经营。"
    )

    result = IntakeService.format_audio_dialogue(raw)

    assert [turn.role for turn in result.turns] == ["operator", "citizen", "operator", "citizen", "operator"]
    assert "商业街晚上摆摊太多" in result.citizen_text
    assert "您说的是哪个区" not in result.citizen_text


def test_confirmed_workorder_handoff_can_be_claimed_and_completed_by_member_b():
    with TestClient(app) as client:
        def login(username: str, password: str) -> dict[str, str]:
            response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
            assert response.status_code == 200
            return {"Authorization": f"Bearer {response.json()['data']['token']}"}

        operator = login("operator", "operator123")
        admin = login("admin", "admin123")
        department_admin = login("cgj_admin", "admin123")
        draft = IntakeService().analyze("镜湖区某小区夜间施工噪声扰民，希望相关部门核查处理。")

        confirmed = client.post(
            "/api/v1/intake/confirm",
            headers=operator,
            json={"workorder": draft.model_dump()},
        )
        assert confirmed.status_code == 200
        confirmed_data = confirmed.json()["data"]
        assert confirmed_data["status"] == "confirmed"
        assert confirmed_data["handoff_status"] == "pending"
        assert confirmed_data["handed_off_at"]

        assert client.get("/api/v1/intake/handoffs", headers=operator).status_code == 403
        assert client.get("/api/v1/intake/handoffs", headers=department_admin).json()["data"] == []
        assert client.post(f"/api/v1/intake/handoffs/{draft.case_id}/claim", headers=department_admin).status_code == 403
        pending = client.get("/api/v1/intake/handoffs?status=pending", headers=admin)
        assert pending.status_code == 200
        assert any(row["case_id"] == draft.case_id for row in pending.json()["data"])

        premature = client.post(
            f"/api/v1/intake/handoffs/{draft.case_id}/result",
            headers=admin,
            json={"classification": {"category": "生态环境"}, "routing": {"dept_ids": ["dept_ecology_environment"]}},
        )
        assert premature.status_code == 409

        claimed = client.post(f"/api/v1/intake/handoffs/{draft.case_id}/claim", headers=admin)
        assert claimed.status_code == 200
        assert claimed.json()["data"]["handoff_status"] == "processing"

        completed = client.post(
            f"/api/v1/intake/handoffs/{draft.case_id}/result",
            headers=admin,
            json={
                "classification": {"category": "生态环境", "confidence": 0.93},
                "routing": {"dept_ids": ["dept_ecology_environment"], "top_k": 1},
                "reply": {"draft": "已转相关部门核查处理。"},
            },
        )
        assert completed.status_code == 200
        result = completed.json()["data"]
        assert result["handoff_status"] == "completed"
        assert result["classification"]["category"] == "生态环境"
        assert result["routing"]["dept_ids"] == ["dept_ecology_environment"]
        assert result["completed_at"]


@pytest.mark.asyncio
async def test_llm_is_primary_when_enabled_and_evidence_is_grounded():
    text = "市民反映昨晚23点镜湖区东方小区二期持续施工噪声扰民，希望立即停止夜间施工。"
    llm = FakeLLM({
        "title": "关于东方小区夜间施工噪声的问题",
        "summary": "东方小区二期夜间施工噪声扰民，群众希望停止夜间施工。",
        "time": "昨晚23点",
        "location": "镜湖区东方小区二期",
        "subjects": [],
        "event": "持续施工噪声扰民",
        "request": "希望立即停止夜间施工",
        "ambiguities": [],
        "clarification_questions": [],
        "evidence": [
            {"field": "time", "quote": "昨晚23点"},
            {"field": "location", "quote": "镜湖区东方小区二期"},
            {"field": "event", "quote": "持续施工噪声扰民"},
            {"field": "request", "quote": "希望立即停止夜间施工"},
        ],
    })
    settings = Settings(intake_llm_enabled=True, deepseek_api_key="test-key")

    result = await LLMIntakeService(llm, settings).analyze(text)

    assert result.generation.mode == "llm"
    assert result.generation.provider == "fake"
    assert result.quality.fidelity == 1.0
    assert "昨晚23点" in result.generation.evidence["time"]
    assert text in llm.messages[-1]["content"]


@pytest.mark.asyncio
async def test_llm_hallucinated_evidence_triggers_rule_fallback():
    llm = FakeLLM({
        "title": "虚构标题", "summary": "虚构摘要", "time": "明天",
        "location": "不存在的地点", "subjects": [], "event": "虚构事件", "request": "虚构诉求",
        "ambiguities": [], "clarification_questions": [],
        "evidence": [{"field": "event", "quote": "原文没有这句话"}],
    })
    settings = Settings(intake_llm_enabled=True, deepseek_api_key="test-key")

    result = await LLMIntakeService(llm, settings).analyze("这里一直很吵，希望相关部门处理。")

    assert result.generation.mode == "rule_fallback"
    assert "证据校验未通过" in result.generation.fallback_reason


@pytest.mark.asyncio
async def test_llm_vague_duration_does_not_replace_received_time():
    llm = FakeLLM({
        "title": "关于施工噪声的问题", "summary": "施工噪声扰民，希望处理。", "time": "一直",
        "location": "镜湖区某小区", "subjects": [], "event": "施工噪声扰民", "request": "希望处理",
        "ambiguities": [], "clarification_questions": [],
        "evidence": [
            {"field": "time", "quote": "一直"}, {"field": "location", "quote": "镜湖区某小区"},
            {"field": "event", "quote": "施工噪声扰民"}, {"field": "request", "quote": "希望处理"},
        ],
    })
    result = await LLMIntakeService(
        llm, Settings(intake_llm_enabled=True, deepseek_api_key="test-key")
    ).analyze(
        "镜湖区某小区一直存在施工噪声扰民，希望处理。",
        received_at="2026-08-27T15:20:00+08:00",
    )
    assert result.elements.time == "2026年8月27日 15:20（按来电时间）"
    assert result.elements.time_basis == "received_at"


@pytest.mark.asyncio
async def test_llm_requires_explicit_data_transfer_switch():
    llm = FakeLLM({})
    result = await LLMIntakeService(llm, Settings(intake_llm_enabled=False)).analyze(
        "镜湖区某小区噪声扰民，希望处理。"
    )
    assert result.generation.mode == "rule_fallback"
    assert result.generation.fallback_reason == "大模型工单生成未授权启用"
    assert llm.messages == []
