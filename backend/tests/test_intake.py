"""12345 端到端工单理解、生成与转派测试。"""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.wuhu import seed_wuhu_departments
from app.harness.agents.dept_router import DeptRouter
from app.intake.llm_service import LLMIntakeService
from app.intake.service import IntakeService
from app.main import app
from app.storage.store import MemoryStore


class FakeLLM:
    api_key = "test-key"
    model = "test-model"

    def __init__(self, payload):
        self.payload = payload
        self.messages = []
        self.calls = 0

    async def complete_json(self, messages, **kwargs):
        self.messages = messages
        payload = self.payload[min(self.calls, len(self.payload) - 1)] if isinstance(self.payload, list) else self.payload
        self.calls += 1
        return payload

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
        department_admin = login("city_management_admin", "admin123")
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


def test_member_b_can_generate_grounded_recommendation_before_result_review():
    with TestClient(app) as client:
        def login(username: str, password: str) -> dict[str, str]:
            response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
            return {"Authorization": f"Bearer {response.json()['data']['token']}"}

        operator = login("operator", "operator123")
        admin = login("admin", "admin123")
        draft = IntakeService().analyze(
            "镜湖区某小区夜间施工噪声影响居民休息，希望核查施工时间并处理。"
        )
        client.post("/api/v1/intake/confirm", headers=operator, json={"workorder": draft.model_dump()})
        citation = {
            "doc_id": "doc-noise", "doc_title": "芜湖市噪声污染防治资料",
            "dept_id": "dept_ecology_environment", "chunk_index": 0,
            "section_path": ["施工噪声"], "snippet": "施工单位应采取噪声污染防治措施。",
        }
        import asyncio
        asyncio.run(client.app.state.container.store.upsert("chunks", {
            "_id": "doc-noise:0", "doc_id": "doc-noise", "chunk_index": 0,
            "content": "施工单位应采取噪声污染防治措施。",
        }))
        client.app.state.container.orchestrator.answer = AsyncMock(return_value={
            "answer": "施工单位应采取噪声污染防治措施。[来源1]建议转交主管部门核查。",
            "citations": [citation], "dept_ids": ["dept_ecology_environment"],
            "confidence": 0.91, "verification": {"passed": True, "score": 0.95, "issues": []},
            "retrieved_count": 5,
        })

        response = client.post(f"/api/v1/intake/handoffs/{draft.case_id}/recommend", headers=admin)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["classification"]["category"] == "生态环境"
        assert data["routing"]["dept_ids"] == ["dept_ecology_environment"]
        assert data["policy_basis"]["citations"][0]["doc_id"] == "doc-noise"
        assert data["reply"]["status"] == "pending_review"
        assert data["release_gate"]["automated_passed"] is True
        assert data["release_gate"]["can_auto_publish"] is False
        assert data["requires_human_review"] is True


def test_handoff_result_rejects_reply_that_fails_compliance_gate():
    with TestClient(app) as client:
        def login(username: str, password: str) -> dict[str, str]:
            response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
            return {"Authorization": f"Bearer {response.json()['data']['token']}"}

        operator = login("operator", "operator123")
        admin = login("admin", "admin123")
        draft = IntakeService().analyze("镜湖区某小区存在施工噪声，希望相关部门处理。")
        client.post("/api/v1/intake/confirm", headers=operator, json={"workorder": draft.model_dump()})
        client.post(f"/api/v1/intake/handoffs/{draft.case_id}/claim", headers=admin)

        response = client.post(
            f"/api/v1/intake/handoffs/{draft.case_id}/result",
            headers=admin,
            json={
                "classification": {"category": "生态环境"},
                "routing": {"dept_ids": ["dept_ecology_environment"]},
                "reply": {"draft": "根据政策规定，我们保证解决，并在7日内办结。", "citations": []},
            },
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["message"] == "回复未通过合规门禁，请修改后重试"
        assert detail["compliance"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_market_pricing_phrase_overrides_generic_fee_routing():
    store = MemoryStore()
    await seed_wuhu_departments(store)
    route = await DeptRouter(FakeLLM({}), store).route(
        "消费者反映理发店未明码标价，结账时收费远高于价目表，希望核查处理。"
    )
    assert route["dept_ids"] == ["dept_market_regulation"]
    assert "未明码标价" in route["reasons"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("query", "expected"), [
    ("小区消防通道被占，救护车无法进入", "dept_public_security"),
    ("家中频繁停电和跳闸，希望检修供电设施", "dept_public_services"),
    ("幼升小报名平台异常，材料无法提交", "dept_education_science_culture_sports"),
    ("咨询企业技术改造补贴申报条件", "dept_economy_trade"),
    ("村里水库水位上涨，需要防汛排查", "dept_agriculture_forestry_water"),
    ("医美机构开展注射项目但未公示执业许可", "dept_health"),
])
async def test_distinctive_12345_matters_route_without_llm(query, expected):
    store = MemoryStore()
    await seed_wuhu_departments(store)
    route = await DeptRouter(FakeLLM({}), store).route(query)
    assert route["dept_ids"][0] == expected
    assert route["matched_by"] == "keyword"


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


def test_audio_generalizes_location_and_event_beyond_legacy_cases():
    transcript = """接线员：您好，请讲。
群众：我在镜湖区某社区卫生服务中心预约，一直显示没有号，电话也没人接。
接线员：您的诉求是什么？
群众：希望核实放号安排，并把咨询电话接通。"""

    result = IntakeService().analyze(transcript, source_type="audio")

    assert result.elements.location == "镜湖区某社区卫生服务中心"
    assert "预约" in result.elements.event
    assert "没有号" in result.elements.event
    assert "接线员" not in result.elements.event
    assert "核实放号安排" in result.elements.request


@pytest.mark.parametrize(("spoken", "expected"), [
    ("静湖区某社区 卫生福务中间", "镜湖区某社区卫生服务中心"),
    ("弯制区某安置小區地下车库", "湾沚区某安置小区地下车库"),
    ("经开区某制造企业", "经开区某制造企业"),
    ("为市某村农田灌溉渠", "无为市某村农田灌溉渠"),
    ("一江区某小区燃气用户", "弋江区某小区燃气用户"),
])
def test_location_normalizes_common_wuhu_asr_variants(spoken, expected):
    assert IntakeService()._extract_location(spoken) == expected


@pytest.mark.parametrize(("spoken", "expected"), [
    ("静湖区某小区", "镜湖区"),
    ("异江区某医院", "弋江区"),
    ("芜湖市某公交站", "市本级"),
])
def test_region_returns_canonical_administrative_area(spoken, expected):
    assert IntakeService()._extract_region(spoken) == expected


@pytest.mark.parametrize(("left", "right", "expected"), [
    ("请问是哪个区经", "开区系统没有提示", "经开区系统没有提示"),
    ("扬尘什么时候严重异", "江区某工地白天最严重", "异江区某工地白天最严重"),
    ("是哪家机构镜", "湖区某社区卫生服务中心", "镜湖区某社区卫生服务中心"),
])
def test_labeled_turns_repair_region_split_across_speaker_boundary(left, right, expected):
    formatted = IntakeService.format_audio_dialogue(f"接线员：{left}\n群众：{right}")

    assert expected in formatted.citizen_text
    assert not formatted.turns[0].text.endswith(left[-1])


@pytest.mark.parametrize(("spoken", "expected_term"), [
    ("场馆周末总是临时闭馆。最好公众号提前发通知。", "最好公众号提前发通知"),
    ("工伤怎么报一直没人讲。想问清楚申报时限和所需材料。", "想问清楚申报时限"),
    ("燃气充值没有到账。麻烦帮我核对充值记录。", "麻烦帮我核对充值记录"),
])
def test_request_extracts_colloquial_action_markers(spoken, expected_term):
    assert expected_term in IntakeService()._extract_request(spoken)


@pytest.mark.asyncio
async def test_llm_ungrounded_field_cannot_overwrite_grounded_rule_field():
    text = "镜湖区某社区卫生服务中心一直预约不到号，希望核实放号安排。"
    llm = FakeLLM({
        "title": "关于预约的问题", "summary": "预约问题。", "time": "",
        "location": "虚构医院", "subjects": [], "event": "一直预约不到号",
        "request": "希望核实放号安排", "ambiguities": [], "clarification_questions": [],
        "evidence": [
            {"field": "event", "quote": "一直预约不到号"},
            {"field": "request", "quote": "希望核实放号安排"},
        ],
    })
    settings = Settings(
        intake_llm_enabled=True, deepseek_api_key="test-key", intake_llm_min_evidence_ratio=0.6
    )

    result = await LLMIntakeService(llm, settings).analyze(text)

    assert result.generation.mode == "llm"
    assert result.elements.location == "镜湖区某社区卫生服务中心"
    assert "虚构医院" not in result.title
    assert "虚构医院" not in result.summary


@pytest.mark.asyncio
async def test_llm_retries_once_when_evidence_schema_is_invalid():
    valid = {
        "title": "关于噪声的问题", "summary": "小区噪声扰民。", "time": "",
        "location": "镜湖区某小区", "subjects": [], "event": "噪声扰民",
        "request": "希望处理", "ambiguities": [], "clarification_questions": [],
        "evidence": [
            {"field": "location", "quote": "镜湖区某小区"},
            {"field": "event", "quote": "噪声扰民"},
            {"field": "request", "quote": "希望处理"},
        ],
    }
    invalid = {**valid, "evidence": [{"field": "ambiguities", "quote": "信息不明"}]}
    llm = FakeLLM([invalid, valid])

    result = await LLMIntakeService(
        llm, Settings(intake_llm_enabled=True, deepseek_api_key="test-key")
    ).analyze("静湖区某小区噪声扰民，希望处理。", source_type="audio")

    assert llm.calls == 2
    assert result.generation.mode == "llm"
    assert result.elements.location == "镜湖区某小区"
    assert any("镜湖区某小区" in message["content"] for message in llm.messages)
