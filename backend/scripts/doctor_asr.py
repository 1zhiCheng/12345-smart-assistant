"""验证本地 ASR 模型能否加载并完成一次脱敏转写。"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from app.config import get_settings
from app.intake.asr import AsrService
from app.intake.service import IntakeService


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    content = args.file.read_bytes()
    started = time.perf_counter()
    result = await AsrService(get_settings()).transcribe(content, args.file.name, "audio/mpeg")
    elapsed = time.perf_counter() - started
    masked = IntakeService.mask_sensitive(result.text)
    intake = IntakeService()
    dialogue = intake.format_audio_dialogue(result.text)
    workorder = intake.analyze(dialogue.formatted_text, source_type="audio", audio_file_name=args.file.name,
                               raw_transcript=result.text)
    target_terms = ["经济技术开发区", "衡山支路", "安徽美芝精密制造有限公司", "商贩摆摊", "绿化围栏", "路灯", "私家车", "非法营运"]
    hits = [term for term in target_terms if term in result.text]
    print(f"ASR_OK provider={result.provider} model={result.model} chars={len(result.text)} seconds={elapsed:.2f}")
    print(f"TERM_HITS={len(hits)}/{len(target_terms)} {json.dumps(hits, ensure_ascii=True)}")
    print(f"DIALOGUE_MODE={dialogue.mode} TURNS={len(dialogue.turns)} CITIZEN_CHARS={len(dialogue.citizen_text)}")
    print("FORMATTED=" + json.dumps(dialogue.formatted_text[:500], ensure_ascii=True))
    print("WORKORDER=" + json.dumps({
        "title": workorder.title,
        "time": workorder.elements.time,
        "location": workorder.elements.location,
        "event": workorder.elements.event,
        "request": workorder.elements.request,
        "content_preview": workorder.content[:240],
    }, ensure_ascii=True))
    print(f"MASKED_PREVIEW={json.dumps(masked[:160], ensure_ascii=True)}")


if __name__ == "__main__":
    asyncio.run(main())
