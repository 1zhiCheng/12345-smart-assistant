import httpx
import numpy as np
import pytest

from app.config import Settings
from app.intake import asr as asr_module
from app.intake.asr import AsrError, AsrService, _normalize_wuhu_terms, _resolve_local_model_path
from app.intake.diarization import DiarizationResult, TimedText, _viterbi_labels


@pytest.mark.asyncio
async def test_asr_requires_configuration():
    settings = Settings(asr_provider="disabled", asr_api_key="")
    with pytest.raises(AsrError, match="尚未配置"):
        await AsrService(settings).transcribe(b"audio", "case.wav", "audio/wav")


@pytest.mark.asyncio
async def test_asr_openai_compatible_response(monkeypatch):
    settings = Settings(
        asr_provider="openai_compatible",
        asr_api_key="test-key",
        asr_base_url="https://asr.example/v1",
        asr_model="whisper-1",
    )

    async def fake_post(self, url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"text": "镜湖区夜间施工噪声扰民"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await AsrService(settings).transcribe(b"audio", "case.wav", "audio/wav")
    assert result.text == "镜湖区夜间施工噪声扰民"
    assert result.model == "whisper-1"


def test_local_asr_is_enabled_without_api_key():
    settings = Settings(asr_provider="faster_whisper", asr_api_key="")
    assert AsrService(settings).enabled is True


def test_local_model_path_is_stable_from_project_or_backend_startup():
    assert _resolve_local_model_path("models/faster-whisper-turbo").name == "faster-whisper-turbo"
    assert _resolve_local_model_path("../models/faster-whisper-turbo").name == "faster-whisper-turbo"


def test_wuhu_domain_homophone_corrections():
    text = "五湖市经开区横山之路美芝精密这一块有白汤，流化胃囊损坏，还有非要营运。"
    corrected = _normalize_wuhu_terms(text)
    assert "芜湖市" in corrected
    assert "经济技术开发区" in corrected
    assert "衡山支路" in corrected
    assert "安徽美芝精密制造有限公司" in corrected
    assert "摆摊" in corrected
    assert "绿化围栏" in corrected
    assert "非法营运" in corrected


def test_diarization_viterbi_removes_short_speaker_flicker():
    segments = [TimedText(i, i + 0.6, word) for i, word in enumerate("abcdefgh")]
    votes = np.array([
        [9, 1], [8, 2], [1, 9], [9, 1], [8, 2], [2, 8], [1, 9], [1, 9],
    ], dtype=float)
    labels = _viterbi_labels(votes, segments)
    assert labels[:5] == [0, 0, 0, 0, 0]
    assert labels[-3:] == [1, 1, 1]


def test_local_asr_remembers_cuda_runtime_fallback(monkeypatch, tmp_path):
    model_path = tmp_path / "model"
    model_path.mkdir()
    calls = []

    class FakeWord:
        start, end, word = 0.0, 0.5, "测试"

    class FakeSegment:
        start, end, text, words = 0.0, 0.5, "测试", [FakeWord()]

    class CudaModel:
        def transcribe(self, *_args, **_kwargs):
            raise RuntimeError("missing cudnn runtime")

    class CpuModel:
        def transcribe(self, *_args, **_kwargs):
            return iter([FakeSegment()]), None

    def fake_load(_path, device, compute_type):
        calls.append((device, compute_type))
        if device == "auto":
            return CudaModel(), "cuda", "float16"
        return CpuModel(), "cpu", "int8"

    monkeypatch.setattr(asr_module, "_load_local_model", fake_load)
    monkeypatch.setattr(
        asr_module,
        "diarize_phone_call",
        lambda *_args, **_kwargs: DiarizationResult((), 0.0, False, "test"),
    )
    service = AsrService(Settings(
        asr_provider="faster_whisper",
        asr_local_model_path=str(model_path),
        asr_local_device="auto",
        asr_local_compute_type="auto",
    ))

    assert service._transcribe_local(b"fake", "case.wav").text == "测试"
    assert service._transcribe_local(b"fake", "case.wav").text == "测试"
    assert calls == [("auto", "auto"), ("cpu", "int8"), ("cpu", "int8")]
