"""在真实录音上检查 ASR 时间片、声学说话人分离与角色映射。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.config import get_settings
from app.intake.asr import AsrService
from app.intake.service import IntakeService


async def evaluate(path: Path) -> dict:
    result = await AsrService(get_settings()).transcribe(path.read_bytes(), path.name, "audio/mpeg")
    dialogue = (
        IntakeService.format_diarized_segments(result.segments)
        if result.diarization_mode == "acoustic"
        else IntakeService.format_audio_dialogue(result.text)
    )
    return {
        "file": str(path),
        "provider": result.provider,
        "model": result.model,
        "segments": len(result.segments),
        "diarization_mode": result.diarization_mode,
        "diarization_confidence": round(result.diarization_confidence, 4),
        "diarization_reason": result.diarization_reason,
        "turns": len(dialogue.turns),
        "operator_turns": sum(turn.role == "operator" for turn in dialogue.turns),
        "citizen_turns": sum(turn.role == "citizen" for turn in dialogue.turns),
        "formatted_text": dialogue.formatted_text,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(evaluate(args.audio.resolve())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
