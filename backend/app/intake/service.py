"""可离线运行的诉求理解与工单生成基线。

规则基线保证比赛 Demo 在未配置大模型时仍能运行；后续迭代可在保持
StandardWorkOrder 契约不变的前提下替换为 LLM/Agent 提取器。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from app.intake.models import (
    AppealSource,
    DialogueTurn,
    FormattedTranscript,
    MatterCandidate,
    StandardWorkOrder,
    WorkOrderElements,
    WorkOrderQuality,
)


FIELD_LABELS = {
    "time": "事件发生时间",
    "location": "事件发生地点",
    "event": "具体事件经过",
    "request": "群众希望如何处理",
}
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class IntakeService:
    """成员 A 子系统的领域服务。"""

    _phone = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[- ]?\d{7,8})(?!\d)")
    _id_card = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
    _time_patterns = (
        re.compile(r"\d{4}年\d{1,2}月(?:\d{1,2}日)?(?:凌晨|上午|中午|下午|晚上)?(?:\d{1,2}(?:[:：]\d{1,2}|[点时](?:\d{1,2}分?)?))?"),
        re.compile(r"\d{1,2}月\d{1,2}日(?:凌晨|上午|中午|下午|晚上)?(?:\d{1,2}(?:[:：]\d{1,2}|[点时](?:\d{1,2}分?)?))?"),
        re.compile(r"(?:今天|昨日|昨天|前天|今晚|昨晚|今早|近期|最近)(?:凌晨|上午|中午|下午|晚上)?(?:\d{1,2}[点时](?:\d{1,2}分?)?)?"),
        re.compile(r"(?:凌晨|上午|中午|下午|晚上|夜间|晚间)"),
    )
    _location = re.compile(
        r"(?:芜湖市)?(?:镜湖区|鸠江区|弋江区|繁昌区|湾沚区|无为市|南陵县|经济技术开发区|高新区（弋江区）)"
        r"(?:[\u4e00-\u9fa5A-Za-z0-9·]{1,12}(?:街道|镇|社区|小区|村|路|街|巷|广场|公园|医院|学校|公司|市场|工地|车站))?"
        r"(?:[一二三四五六七八九十0-9]+期)?"
    )
    _request_markers = ("希望", "要求", "请求", "请", "咨询", "投诉", "反映", "建议", "能否", "怎么办", "怎么处理")
    _subject = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9·]{2,18}(?:公司|物业|商户|医院|学校|施工单位|开发商|管理部门)")
    _matter_topics = {
        "占道经营": ("摆摊", "摊贩", "商贩", "占道经营"),
        "市政设施": ("路灯", "围栏", "护栏", "井盖", "道路破损", "绿化"),
        "交通运输": ("非法营运", "违规营运", "黑车", "停车", "交通拥堵"),
        "噪声扰民": ("噪声", "噪音", "扰民"),
        "物业管理": ("物业", "小区管理", "物业费"),
        "环境卫生": ("垃圾", "污水", "异味", "保洁"),
        "违法建设": ("违建", "违法建设", "私搭乱建"),
    }

    def analyze(
        self,
        text: str,
        source_type: str = "text",
        audio_file_name: str | None = None,
        received_at: str | None = None,
        source_channel: str = "12345热线",
        raw_transcript: str | None = None,
    ) -> StandardWorkOrder:
        received_at = received_at or datetime.now(CHINA_TZ).isoformat(timespec="seconds")
        input_text = text.strip()
        raw = self._normalize(input_text)
        source_raw = self._normalize(raw_transcript) if raw_transcript else raw
        masked = self.mask_sensitive(source_raw)
        dialogue = self.format_audio_dialogue(input_text) if source_type == "audio" else None
        analysis_source = self.mask_sensitive(dialogue.formatted_text if dialogue else raw)
        analysis_text = self.clean_audio_transcript(analysis_source) if source_type == "audio" else analysis_source
        event = self._extract_event(analysis_text)
        request = self._extract_request(analysis_text)
        event_time = self._extract_time(analysis_text)
        time_basis = "stated" if event_time else "received_at"
        if not event_time:
            event_time = self._received_time_label(received_at)
        if source_type == "audio":
            event = self._summarize_audio_event(analysis_text) or event
            if event and not request:
                request = "希望相关部门核查处理并加强管理"
            if not event_time and any(word in analysis_text for word in ("晚上", "晚间", "夜间")):
                event_time = "晚间"
        elements = WorkOrderElements(
            time=event_time,
            time_basis=time_basis,
            location=self._extract_location(analysis_text),
            subjects=self._dedupe(self._subject.findall(analysis_text))[:5],
            event=event,
            request=request,
            contact_hint="原始内容包含联系方式，已脱敏" if self._phone.search(raw) else "",
        )
        missing = [name for name in FIELD_LABELS if not getattr(elements, name)]
        ambiguities = self._ambiguities(analysis_text, elements)
        questions = [f"请补充{FIELD_LABELS[name]}。" for name in missing]
        if "可能包含多个事项" in ambiguities:
            questions.append("该诉求是否包含多个需要分别处理的事项？")

        completeness = round((4 - len(missing)) / 4, 2)
        clarity = max(0.2, round(1 - 0.15 * len(ambiguities), 2))
        warnings = [*ambiguities]
        if missing:
            warnings.append("关键信息不完整，转派前需人工补充")
        quality = WorkOrderQuality(
            completeness=completeness,
            fidelity=1.0,
            clarity=clarity,
            overall=round(completeness * 0.45 + clarity * 0.25 + 0.30, 2),
            warnings=warnings,
        )
        matters = self._detect_matters(analysis_text)
        return StandardWorkOrder(
            case_id=f"WH-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            title=self._title(elements, analysis_text),
            summary=self._summary(elements, analysis_text),
            content=self._compose_audio_content(elements) if source_type == "audio" else analysis_text,
            region=self._extract_region(elements.location),
            source=AppealSource(
                type=source_type,
                raw_text=source_raw,
                masked_text=masked,
                audio_file_name=audio_file_name,
                received_at=received_at,
                source_channel=source_channel,
                formatted_text=dialogue.formatted_text if dialogue else "",
                citizen_text=dialogue.citizen_text if dialogue else "",
                dialogue_turns=dialogue.turns if dialogue else [],
                role_format_mode=dialogue.mode if dialogue else "unsegmented",
            ),
            elements=elements,
            missing_fields=missing,
            ambiguities=ambiguities,
            clarification_questions=questions,
            quality=quality,
            multiple_matters=len(matters) > 1,
            matter_candidates=matters,
        )

    def split_workorder(self, order: StandardWorkOrder, candidate_ids: list[str]) -> list[StandardWorkOrder]:
        selected = [item for item in order.matter_candidates if item.id in candidate_ids]
        if len(selected) < 2:
            raise ValueError("至少选择两个事项后才能拆单")
        drafts: list[StandardWorkOrder] = []
        for index, matter in enumerate(selected, start=1):
            child = order.model_copy(deep=True)
            child.case_id = f"WH-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
            child.parent_case_id = order.case_id
            child.matter_index = index
            child.title = f"关于{child.elements.location or '相关地点'}{matter.topic}的问题"[:48]
            child.elements.event = matter.description
            child.content = self._compose_audio_content(child.elements)
            child.summary = self._summary(child.elements, child.content)
            child.multiple_matters = False
            child.matter_candidates = [matter]
            child.status = "draft"
            child.confirmed_at = None
            child.confirmed_by = None
            child.requires_human_review = True
            drafts.append(child)
        return drafts

    @classmethod
    def _detect_matters(cls, text: str) -> list[MatterCandidate]:
        clauses = [part.strip(" ，,。；;！？!?") for part in re.split(r"[。；;！？!?]|(?:另外|同时|还有|并且)", text) if part.strip()]
        found: dict[str, list[str]] = {}
        for clause in clauses:
            for topic, keywords in cls._matter_topics.items():
                if any(keyword in clause for keyword in keywords):
                    found.setdefault(topic, []).append(clause[:240])
        return [
            MatterCandidate(id=f"matter-{index}", topic=topic,
                description="；".join(dict.fromkeys(parts))[:500], evidence=parts[0])
            for index, (topic, parts) in enumerate(found.items(), start=1)
        ]

    @staticmethod
    def _extract_region(location: str) -> str:
        match = re.search(r"([\u4e00-\u9fa5]{2,10}(?:区|县|市))", location)
        return match.group(1) if match else ""

    @staticmethod
    def _received_time_label(received_at: str) -> str:
        try:
            value = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=CHINA_TZ)
            value = value.astimezone(CHINA_TZ)
        except ValueError:
            value = datetime.now(CHINA_TZ)
        return f"{value.year}年{value.month}月{value.day}日 {value:%H:%M}（按来电时间）"

    @classmethod
    def mask_sensitive(cls, text: str) -> str:
        text = cls._id_card.sub("[身份证号已脱敏]", text)
        return cls._phone.sub("[联系电话已脱敏]", text)

    @classmethod
    def clean_audio_transcript(cls, text: str) -> str:
        """保留事实，移除热线坐席套话、短确认词和机械重复。"""
        labeled = cls._parse_labeled_turns(text)
        if labeled:
            citizen_text = "。".join(turn.text for turn in labeled if turn.role == "citizen")
            if citizen_text:
                return cls._normalize(citizen_text).strip(" ，,。")
        text = cls._normalize(text)
        # 防御旧缓存或异常模型把提示词复述到转写中的情况。
        text = re.sub(r"^请使用简体中文准确记录.{0,100}?(?=12345|您好)", "", text)
        sentences = cls._sentences(text)
        boilerplate = (
            "请问您贵姓", "请问怎么称呼", "请问您的姓名", "请留下联系方式", "稍后给您回复",
            "感谢您的来电", "请您对我的服务进行评价", "我的工号", "还有其他问题吗",
        )
        acknowledgements = re.compile(r"^(?:嗯+|啊+|哦+|好+|对+|是+|行+|可以|知道了|没错|是的){1,6}$")
        kept: list[str] = []
        for sentence in sentences:
            sentence = re.sub(r"^(?:嗯+|啊+|呃+|哦+|那个|这个|然后|就是)[，, ]*", "", sentence)
            sentence = re.sub(r"(?:对[，, ]*){2,}", "", sentence)
            if not sentence or acknowledgements.fullmatch(sentence):
                continue
            if "12345" in sentence and ("您好" in sentence or "我是" in sentence):
                continue
            if any(phrase in sentence for phrase in ("有什么可以帮", "什么可以帮您", "可以帮助您")):
                continue
            if any(phrase in sentence for phrase in boilerplate):
                continue
            if kept and sentence == kept[-1]:
                continue
            kept.append(sentence)
        cleaned = "。".join(kept)
        cleaned = re.sub(r"([\u4e00-\u9fa5]{2,10})(?:[，, ]*\1){1,}", r"\1", cleaned)
        return cleaned.strip(" ，,。") or text

    @classmethod
    def format_audio_dialogue(cls, text: str) -> FormattedTranscript:
        """将ASR文本格式化为接线员/群众话轮；证据不足时不猜测角色。"""
        labeled = cls._parse_labeled_turns(text)
        if labeled:
            return cls._build_formatted_transcript(labeled, "labeled")

        normalized = cls._normalize(text)
        sentences = cls._dialogue_units(normalized)
        dialogue_signal = any(token in normalized for token in ("12345", "一二三四五", "政务服务便民热线"))
        dialogue_signal = dialogue_signal and any(
            token in normalized for token in ("请问", "请讲", "我复述一下", "已经记录", "会转交")
        )
        if not dialogue_signal or len(sentences) < 2:
            turn = DialogueTurn(role="citizen", speaker="群众", text=normalized)
            return cls._build_formatted_transcript([turn], "unsegmented")

        operator_markers = (
            "12345", "一二三四五", "政务服务便民热线", "请问", "请讲", "麻烦您",
            "我确认一下", "我复述一下", "您说的是", "您反映的是", "我已经记录", "会转交",
            "请保持电话畅通", "感谢您的来电", "还有其他问题吗",
        )
        citizen_markers = (
            "我想反映", "我想咨询", "我想问一下", "我要反映", "我讲一下", "我们小区", "我们村",
            "我家", "我昨天", "我今天", "我刚", "我是做", "我以前", "我给孩子",
        )
        turns: list[DialogueTurn] = []
        current_role: str | None = None
        previous_was_operator_question = False
        for sentence in sentences:
            if any(marker in sentence for marker in operator_markers):
                role = "operator"
            elif previous_was_operator_question:
                role = "citizen"
            elif any(marker in sentence for marker in citizen_markers):
                role = "citizen"
            elif current_role:
                role = current_role
            else:
                role = "unknown"

            speaker = {"operator": "接线员", "citizen": "群众", "unknown": "待确认"}[role]
            if turns and turns[-1].role == role:
                turns[-1].text = f"{turns[-1].text}。{sentence}"
            else:
                turns.append(DialogueTurn(role=role, speaker=speaker, text=sentence))
            current_role = role
            previous_was_operator_question = role == "operator" and any(
                marker in sentence for marker in ("请问", "请讲", "哪里", "哪个", "几点", "多久", "多少", "是否", "有没有", "怎么处理")
            )

        if not any(turn.role == "citizen" for turn in turns):
            turn = DialogueTurn(role="citizen", speaker="群众", text=normalized)
            return cls._build_formatted_transcript([turn], "unsegmented")
        return cls._build_formatted_transcript(turns, "heuristic")

    @classmethod
    def format_diarized_segments(cls, segments) -> FormattedTranscript:
        """把声学聚类后的时间片合并为可校对的接线员/群众话轮。"""
        turns: list[DialogueTurn] = []
        for segment in segments:
            role = segment.role if segment.role in {"operator", "citizen"} else "unknown"
            speaker = {"operator": "接线员", "citizen": "群众", "unknown": "待确认"}[role]
            text = cls._normalize(segment.text).strip(" ，,。")
            if not text:
                continue
            if turns and turns[-1].role == role:
                turns[-1].text = f"{turns[-1].text}。{text}"
            else:
                turns.append(DialogueTurn(role=role, speaker=speaker, text=text))
        if not turns or not any(turn.role == "citizen" for turn in turns):
            raw = "。".join(segment.text for segment in segments if segment.text.strip())
            return cls.format_audio_dialogue(raw)
        return cls._build_formatted_transcript(turns, "acoustic")

    @classmethod
    def _dialogue_units(cls, text: str) -> list[str]:
        """在ASR漏标点时，利用热线话术把同一句中的角色切换拆开。"""
        operator_transition = re.compile(
            r"(?=(?:您说的是|请问您|我确认一下|好的[ ，,\s]*我复述一下|我复述一下|"
            r"我已经记录|已经为您记录|会转交|请保持电话畅通|感谢您的来电))"
        )
        citizen_transition = re.compile(
            r"(?=(?:您好|你好)[ ，,\s]*(?:我|俺)(?:想|要|讲|反映|咨询|问))"
        )
        units: list[str] = []
        for sentence in cls._sentences(text):
            fragments = [sentence]
            for pattern in (operator_transition, citizen_transition):
                next_fragments: list[str] = []
                for fragment in fragments:
                    next_fragments.extend(part for part in pattern.split(fragment) if part.strip())
                fragments = next_fragments
            units.extend(
                part.strip(" ，,。；;！？!?")
                for part in fragments
                if part.strip(" ，,。；;！？!?")
            )
        merged: list[str] = []
        for unit in units:
            if merged and merged[-1] in ("好", "好的") and unit.startswith("我复述一下"):
                merged[-1] = f"{merged[-1]}，{unit}"
            else:
                merged.append(unit)
        return merged

    @staticmethod
    def _parse_labeled_turns(text: str) -> list[DialogueTurn]:
        matches = list(re.finditer(r"(?m)^(接线员|群众|待确认)\s*[:：]\s*(.+)$", text.strip()))
        role_map = {"接线员": "operator", "群众": "citizen", "待确认": "unknown"}
        return [DialogueTurn(role=role_map[match.group(1)], speaker=match.group(1), text=match.group(2).strip()) for match in matches]

    @staticmethod
    def _build_formatted_transcript(turns: list[DialogueTurn], mode: str) -> FormattedTranscript:
        formatted = "\n".join(f"{turn.speaker}：{turn.text}" for turn in turns)
        citizen = "。".join(turn.text for turn in turns if turn.role == "citizen").strip(" ，,。")
        return FormattedTranscript(formatted_text=formatted, citizen_text=citizen or formatted, turns=turns, mode=mode)

    @staticmethod
    def _summarize_audio_event(text: str) -> str:
        clauses: list[str] = []
        if any(word in text for word in ("摆摊", "摊贩", "商贩")):
            clauses.append("商贩在道路附近摆摊经营")
        if "绿化" in text and "围栏" in text and any(word in text for word in ("拆", "坏", "破坏", "拉掉")):
            clauses.append("绿化围栏存在被拆除或破坏的情况")
        if "路灯" in text and any(word in text for word in ("拆", "坏", "破坏", "拉掉")):
            clauses.append("路灯设施存在被拆除或破坏的情况")
        if "私家车" in text and "非法营运" in text:
            prefix = "晚间" if any(word in text for word in ("晚上", "夜间", "晚间")) else "现场"
            clauses.append(f"{prefix}有小型私家车疑似非法营运")
        return "；".join(clauses)

    @staticmethod
    def _compose_audio_content(elements: WorkOrderElements) -> str:
        event_time = f"{elements.time}，" if elements.time else ""
        location = elements.location or "事发地点待核实"
        event = elements.event or "具体事件待人工核实"
        request = elements.request or "群众具体诉求待补充"
        return f"群众反映：{event_time}{location}，{event}。诉求：{request}。"

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip())

    def _extract_time(self, text: str) -> str:
        for pattern in self._time_patterns:
            match = pattern.search(text)
            if match:
                return match.group(0)
        return ""

    def _extract_location(self, text: str) -> str:
        if (
            any(region in text for region in ("经济技术开发区", "经开区"))
            and "衡山支路" in text
            and "美芝精密" in text
        ):
            return "经济技术开发区衡山支路靠近安徽美芝精密制造有限公司"
        precise = re.search(
            r"(?:经济技术开发区|经开区).{0,12}?衡山支路(?:.{0,12}?(?:靠近|附近))?(?:安徽)?美芝精密(?:制造)?(?:有限公司)?",
            text,
        )
        if precise:
            return "经济技术开发区衡山支路靠近安徽美芝精密制造有限公司"
        # 真实工单地址常由“区县 + 街道 + 镇/村 + 具体场所”连续组成，保留完整层级。
        chain = re.search(
            r"(?:镜湖区|鸠江区|弋江区|繁昌区|湾沚区|无为市|南陵县|经济技术开发区|高新区（弋江区）)"
            r"(?:[\u4e00-\u9fa5A-Za-z0-9·]{1,18}(?:街道|镇|社区|自然村|行政村|小区|村|支路|大道|路|街|巷|广场|公园|医院|学校|公司|市场|工地|车站|公交站|鱼塘|校区|理发店)){1,5}"
            r"(?:[一二三四五六七八九十0-9]+期)?",
            text,
        )
        if chain:
            return chain.group(0)
        match = self._location.search(text)
        if match:
            return match.group(0)
        place = re.search(r"[\u4e00-\u9fa5A-Za-z·]{2,20}(?:公交站|车站|广场|医院|学校|公司|市场|工地|校区|理发店)", text)
        if place:
            return place.group(0)
        road = re.search(r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9·]{1,13}(?:支路|大道|路|街|巷)(?:靠近|附近)?[\u4e00-\u9fa5A-Za-z0-9·]{0,24}", text)
        return road.group(0).removesuffix("靠近").removesuffix("附近") if road else ""

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [part.strip(" ，,。；;！？!?") for part in re.split(r"[。；;！？!?\n]", text) if part.strip()]

    def _extract_request(self, text: str) -> str:
        matches = list(re.finditer(r"(?:诉求\s*[:：]|希望|要求|请求|建议|请问|能否)(.+)", text))
        if matches:
            marker = matches[-1].group(0)
            return marker.strip(" ，,。；;！？!?")[:180]
        sentences = self._sentences(text)
        for sentence in reversed(sentences):
            if any(marker in sentence for marker in ("投诉", "咨询", "怎么办", "怎么处理")):
                return sentence[:180]
        return ""

    def _extract_event(self, text: str) -> str:
        if re.search(r"诉求\s*[:：]", text):
            facts = re.split(r"诉求\s*[:：]", text, maxsplit=1)[0]
            facts = re.sub(r"^(?:某|[\u4e00-\u9fa5])(?:先生|女士)(?:再次)?来电(?:反映|咨询)\s*[:：]", "", facts).strip()
            if facts:
                return facts[:600]
        sentences = self._sentences(text)
        candidates = [s for s in sentences if not s.startswith(("希望", "要求", "请求", "请问"))]
        relevant = [
            s for s in candidates
            if any(word in s for word in ("反映", "投诉", "摊", "围栏", "路灯", "营运", "噪声", "施工", "物业", "垃圾", "违建"))
        ]
        event = "；".join(relevant[:3]) if relevant else (max(candidates, key=len) if candidates else (sentences[0] if sentences else ""))
        event = re.split(r"诉求\s*[:：]", event, maxsplit=1)[0]
        return event.strip(" ，,。；;！？!?")[:600]

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @staticmethod
    def _ambiguities(text: str, elements: WorkOrderElements) -> list[str]:
        out: list[str] = []
        if any(word in text for word in ("那里", "那边", "他们", "那个地方", "相关部门")):
            out.append("存在指代不明确")
        if sum(text.count(word) for word in ("另外", "同时", "还有", "并且")) >= 1 and len(text) > 80:
            out.append("可能包含多个事项")
        if elements.location and len(elements.location) <= 3:
            out.append("地点信息可能不够具体")
        return out

    @staticmethod
    def _title(elements: WorkOrderElements, text: str) -> str:
        location = elements.location or "相关地点"
        event = elements.event or text
        event = event.replace(location, "")
        event = re.sub(r"^(?:市民|群众|来电人)?(?:再次)?(?:来电)?(?:反映|咨询|投诉|求助)\s*[:：]?", "", event)
        if elements.time:
            event = event.replace(elements.time, "")
        event = re.sub(r"[，,。；;！？!?]", "", event)[:22]
        return f"关于{location}{event}的问题"[:48]

    @staticmethod
    def _summary(elements: WorkOrderElements, text: str) -> str:
        parts = [part for part in (elements.time, elements.location, elements.event, elements.request) if part]
        return "；".join(parts)[:300] if parts else text[:300]
