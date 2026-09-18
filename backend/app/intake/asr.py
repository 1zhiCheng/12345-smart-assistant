"""录音转写适配器：通过 OpenAI 兼容的 audio/transcriptions 接口调用 ASR。"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from app.config import Settings
from app.intake.diarization import TimedText, diarize_phone_call


SUPPORTED_AUDIO_SUFFIXES = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg"}
HOTWORDS_PATH = Path(__file__).with_name("wuhu_hotwords.txt")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AsrError(RuntimeError):
    """可安全展示给前端的语音转写错误。"""


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    role: str = "unknown"
    speaker_id: str = ""


@dataclass(frozen=True)
class Transcript:
    text: str
    provider: str
    model: str
    segments: tuple[TranscriptSegment, ...] = ()
    diarization_mode: str = "none"
    diarization_confidence: float = 0.0
    diarization_reason: str = ""


class AsrService:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Some Windows hosts expose a CUDA device to ctranslate2 while the
        # required CUDA/cuDNN runtime is incomplete.  Once a real inference
        # fails, remember the fallback for this service instance instead of
        # retrying the same unstable GPU path for every recording.
        self._force_local_cpu = False

    @property
    def enabled(self) -> bool:
        if self.settings.asr_provider == "faster_whisper":
            return True
        return self.settings.asr_provider == "openai_compatible" and bool(self.settings.asr_api_key)

    async def transcribe(self, content: bytes, file_name: str, content_type: str | None) -> Transcript:
        if self.settings.asr_provider == "faster_whisper":
            return await asyncio.to_thread(self._transcribe_local, content, file_name)
        if not self.enabled:
            raise AsrError("语音转写服务尚未配置，请设置 ASR_API_KEY 后重启后端，或在文本框中人工补录。")

        url = f"{self.settings.asr_base_url.rstrip('/')}/audio/transcriptions"
        try:
            async with httpx.AsyncClient(timeout=self.settings.asr_timeout) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.settings.asr_api_key}"},
                    data={"model": self.settings.asr_model, "language": self.settings.asr_language},
                    files={"file": (file_name, content, content_type or "application/octet-stream")},
                )
        except httpx.TimeoutException as exc:
            raise AsrError("语音转写超时，请重试或在文本框中人工补录。") from exc
        except httpx.HTTPError as exc:
            raise AsrError("无法连接语音转写服务，请稍后重试或人工补录。") from exc

        if response.status_code >= 400:
            raise AsrError(f"语音转写服务返回错误（HTTP {response.status_code}），请检查配置或人工补录。")

        try:
            text = str(response.json().get("text", "")).strip()
        except (ValueError, AttributeError) as exc:
            raise AsrError("语音转写服务返回了无法识别的结果。") from exc
        if not text:
            raise AsrError("未从录音中识别出有效文字，请检查录音质量或人工补录。")
        return Transcript(text=text, provider=self.settings.asr_provider, model=self.settings.asr_model)

    def _transcribe_local(self, content: bytes, file_name: str) -> Transcript:
        model_path = _resolve_local_model_path(self.settings.asr_local_model_path)
        if not model_path.exists():
            raise AsrError(f"本地语音模型不存在：{model_path}。请先下载 turbo 模型。")

        suffix = Path(file_name).suffix or ".wav"
        temp_path = ""
        try:
            with NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                temp_file.write(content)
                temp_path = temp_file.name
            requested_device = "cpu" if self._force_local_cpu else self.settings.asr_local_device
            requested_compute = "int8" if self._force_local_cpu else self.settings.asr_local_compute_type
            model, device, compute_type = _load_local_model(str(model_path), requested_device, requested_compute)
            try:
                hotwords = _load_hotwords()
                segments, _ = model.transcribe(
                    temp_path,
                    language=self.settings.asr_language,
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 250},
                    condition_on_previous_text=False,
                    hotwords=hotwords,
                    temperature=0.0,
                    repetition_penalty=1.12,
                    no_repeat_ngram_size=4,
                    word_timestamps=True,
                )
                raw_segments = list(segments)
            except RuntimeError as exc:
                if device != "cuda":
                    raise
                # CUDA/cuDNN 运行库不完整时保证演示仍可用。
                self._force_local_cpu = True
                model, device, compute_type = _load_local_model(str(model_path), "cpu", "int8")
                segments, _ = model.transcribe(
                    temp_path,
                    language=self.settings.asr_language,
                    beam_size=5,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 250},
                    condition_on_previous_text=False,
                    hotwords=_load_hotwords(),
                    temperature=0.0,
                    repetition_penalty=1.12,
                    no_repeat_ngram_size=4,
                    word_timestamps=True,
                )
                raw_segments = list(segments)

            sentence_segments = tuple(
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=_normalize_wuhu_terms(segment.text.strip(" ，,。")),
                )
                for segment in raw_segments
                if segment.text.strip()
            )
            word_segments = tuple(
                TranscriptSegment(
                    start=float(word.start),
                    end=float(word.end),
                    text=_normalize_wuhu_terms(word.word),
                )
                for segment in raw_segments
                for word in (segment.words or [])
                if word.start is not None and word.end is not None and word.word.strip()
            )
            timed_segments = word_segments if len(word_segments) >= 4 else sentence_segments
            text = "。".join(segment.text for segment in sentence_segments).strip()
            diarization = diarize_phone_call(
                temp_path,
                [TimedText(start=segment.start, end=segment.end, text=segment.text) for segment in timed_segments],
                self.settings.asr_speaker_model_path,
            )
            if diarization.accepted:
                timed_segments = tuple(
                    TranscriptSegment(
                        start=segment.start,
                        end=segment.end,
                        text=segment.text,
                        role=segment.role,
                        speaker_id=segment.speaker_id,
                    )
                    for segment in diarization.segments
                )
        except AsrError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AsrError(f"本地语音转写失败：{exc}") from exc
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

        if not text:
            raise AsrError("未从录音中识别出有效文字，请检查录音质量或人工补录。")
        return Transcript(
            text=_normalize_wuhu_terms(text),
            provider="faster_whisper",
            model=f"turbo ({device}/{compute_type})",
            segments=timed_segments,
            diarization_mode="acoustic" if diarization.accepted else "none",
            diarization_confidence=diarization.confidence,
            diarization_reason=diarization.reason,
        )


def _resolve_local_model_path(configured_path: str) -> Path:
    """相对路径兼容从项目根目录或 backend 目录启动服务。"""
    configured = Path(configured_path).expanduser()
    if configured.is_absolute():
        return configured.resolve()
    candidates = (
        (Path.cwd() / configured).resolve(),
        (PROJECT_ROOT / configured).resolve(),
        (PROJECT_ROOT / "backend" / configured).resolve(),
    )
    return next((path for path in candidates if path.exists()), candidates[1])


@lru_cache(maxsize=3)
def _load_local_model(model_path: str, device: str, compute_type: str):
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AsrError("尚未安装 faster-whisper，请先安装后端语音依赖。") from exc

    selected_device = device
    selected_compute = compute_type
    if device == "auto":
        selected_device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    if compute_type == "auto":
        selected_compute = "float16" if selected_device == "cuda" else "int8"
    try:
        model = WhisperModel(model_path, device=selected_device, compute_type=selected_compute, local_files_only=True)
    except RuntimeError:
        if selected_device != "cuda":
            raise
        selected_device, selected_compute = "cpu", "int8"
        model = WhisperModel(model_path, device=selected_device, compute_type=selected_compute, local_files_only=True)
    return model, selected_device, selected_compute


@lru_cache(maxsize=1)
def _load_hotwords() -> str:
    if not HOTWORDS_PATH.exists():
        return ""
    return " ".join(line.strip() for line in HOTWORDS_PATH.read_text(encoding="utf-8").splitlines() if line.strip())


def _normalize_wuhu_terms(text: str) -> str:
    """校正比赛电话录音中稳定出现的芜湖地名和政务事项同音误识别。"""
    replacements = (
        (r"(?:简开区|剪开区|经开发区|经济开发区|经开区)", "经济技术开发区"),
        (r"(?:五湖市|鲜湖市)", "芜湖市"),
        (r"(?:五湖|鲜湖)(?=(?:12345|一二三四五|热线))", "芜湖"),
        (r"静湖区", "镜湖区"),
        (r"垃圾(?:头|投)放点", "垃圾投放点"),
        (r"新门口", "西门口"),
        (r"一大早就充电", "一大早就冲地"),
        (r"(?:横山之路|恒山之路|恒山直路|衡山之路|河南之路)", "衡山支路"),
        (r"(?:安徽)?美芝精密(?:制造)?(?:有限公司)?", "安徽美芝精密制造有限公司"),
        (r"(?:白汤|摆汤)(?=[的呢，,。 ]|$)", "摆摊"),
        (r"(?:流化|硫化)(?=围栏|护栏|胃栏|胃囊|围囊|上面|带|全部)", "绿化"),
        (r"(?:胃栏|胃囊|围囊)", "围栏"),
        (r"(?:非要营运|非法运营)", "非法营运"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text
