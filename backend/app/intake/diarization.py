"""轻量级离线电话录音说话人分离。

针对比赛数据中的 8 kHz 单声道双人热线录音：从 Whisper 时间片提取 MFCC、
基频和频谱统计，聚类为两个声纹，再依据热线话术映射为接线员/群众。
它不替代专业声纹模型，但比仅凭转写文本猜测角色可靠，并可完全离线运行。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

import numpy as np
from scipy.fft import dct


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CAMPPLUS_MODEL = PROJECT_ROOT / "models" / "speaker-diarization" / "campplus.onnx"


@dataclass(frozen=True)
class TimedText:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class DiarizedText(TimedText):
    role: str
    speaker_id: str


@dataclass(frozen=True)
class DiarizationResult:
    segments: tuple[DiarizedText, ...]
    confidence: float
    accepted: bool
    reason: str = ""


OPERATOR_MARKERS = (
    "12345", "345热线", "热线", "有什么可以帮", "什么可以帮", "请问", "请讲",
    "麻烦您", "您贵姓", "怎么称呼", "具体地址", "哪个区", "哪个小区", "哪条路",
    "我确认一下", "我复述一下", "您反映的是", "已经记录", "为您记录", "会转交",
    "联系您", "保持电话畅通", "还有其他", "感谢您的来电",
)
CITIZEN_MARKERS = (
    "我想反映", "我要反映", "我想咨询", "我想问", "我投诉", "我举报", "我家",
    "我们小区", "我们村", "我们这边", "我昨天", "我今天", "我刚才", "希望处理",
    "帮我处理", "一直没人管", "这个问题", "那个地方",
)


def diarize_phone_call(
    audio_path: str | Path,
    segments: list[TimedText],
    model_path: str | Path | None = None,
) -> DiarizationResult:
    """使用 CAM++ 短窗声纹对 Whisper 词时间戳做双人聚类。"""
    if len(segments) < 4:
        return DiarizationResult((), 0.0, False, "有效语音片段不足")

    try:
        samples, sample_rate = _decode_mono(audio_path)
        configured_model = _resolve_model_path(model_path) if model_path else DEFAULT_CAMPPLUS_MODEL
        matrix, centers, neural = _embedding_windows(samples, sample_rate, segments, configured_model)
        if len(matrix) < 6:
            return DiarizationResult((), 0.0, False, "可用于声纹聚类的片段不足")

        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score

        clusterer = KMeans(n_clusters=2, n_init=20, random_state=2026)
        labels = clusterer.fit_predict(matrix)
        if min(np.bincount(labels, minlength=2)) < 2:
            return DiarizationResult((), 0.0, False, "其中一个声纹样本过少")
        silhouette = float(silhouette_score(matrix, labels, metric="cosine" if neural else "euclidean"))

        # 每个词按最近的多个短窗投票，降低恰好跨越说话人切换点的窗口影响。
        vote_rows: list[list[float]] = []
        for item in segments:
            midpoint = (item.start + item.end) / 2.0
            nearest = np.argsort(np.abs(centers - midpoint))[:5]
            distances = np.maximum(np.abs(centers[nearest] - midpoint), 0.05)
            votes = [float(np.sum(1.0 / distances[labels[nearest] == cluster])) for cluster in (0, 1)]
            vote_rows.append(votes)
        all_labels = _viterbi_labels(np.asarray(vote_rows, dtype=np.float64), segments)

        role_by_cluster, role_score = _map_roles(segments, all_labels)
        confidence = max(0.0, min(1.0, 0.78 * max(0.0, silhouette) + 0.22 * role_score))
        min_silhouette = 0.045 if neural else 0.035
        if silhouette < min_silhouette or role_score < 0.08:
            return DiarizationResult((), confidence, False, "双声纹或热线角色证据不足")

        output = _merge_labeled_text(segments, all_labels, role_by_cluster)
        return DiarizationResult(output, confidence, True)
    except Exception as exc:  # noqa: BLE001 - 分离失败必须安全降级，不影响 ASR 主链路
        return DiarizationResult((), 0.0, False, f"声学分离失败：{type(exc).__name__}")


def _map_roles(segments: list[TimedText], labels: list[int]) -> tuple[dict[int, str], float]:
    scores = {0: 0.0, 1: 0.0}
    evidence = {0: 0, 1: 0}
    cluster_text = {
        cluster: re.sub(r"\s+", "", "".join(item.text for item, label in zip(segments, labels, strict=True) if label == cluster))
        for cluster in (0, 1)
    }
    for label, text in cluster_text.items():
        operator_hits = sum(marker in text for marker in OPERATOR_MARKERS)
        citizen_hits = sum(marker in text for marker in CITIZEN_MARKERS)
        question_bonus = 0.35 * sum(text.count(marker) for marker in ("哪里", "哪个", "几点", "多久", "多少", "有没有"))
        scores[label] += 1.25 * operator_hits + question_bonus - 0.8 * citizen_hits
        evidence[label] += operator_hits + citizen_hits
    first_text = re.sub(r"\s+", "", "".join(item.text for item in segments[:12]))
    if any(marker in first_text for marker in ("热线", "有什么可以帮", "您好")):
        scores[labels[0]] += 1.2

    operator_cluster = max(scores, key=scores.get)
    citizen_cluster = 1 - operator_cluster
    gap = scores[operator_cluster] - scores[citizen_cluster]
    evidence_total = max(1, evidence[0] + evidence[1])
    role_score = max(0.0, min(1.0, gap / max(2.0, evidence_total * 0.8)))
    return {operator_cluster: "operator", citizen_cluster: "citizen"}, role_score


def _merge_labeled_text(
    segments: list[TimedText], labels: list[int], role_by_cluster: dict[int, str]
) -> tuple[DiarizedText, ...]:
    merged: list[DiarizedText] = []
    for item, label in zip(segments, labels, strict=True):
        if not item.text.strip():
            continue
        role = role_by_cluster[label]
        if merged and merged[-1].role == role:
            previous = merged[-1]
            gap = item.start - previous.end
            separator = "。" if gap > 0.45 else ""
            merged[-1] = DiarizedText(
                start=previous.start,
                end=item.end,
                text=f"{previous.text}{separator}{item.text}",
                role=role,
                speaker_id=previous.speaker_id,
            )
        else:
            merged.append(DiarizedText(item.start, item.end, item.text, role, f"speaker_{label + 1}"))
    return tuple(merged)


def _viterbi_labels(votes: np.ndarray, segments: list[TimedText]) -> list[int]:
    """用连续性约束抑制滑窗边界附近的角色抖动。"""
    probabilities = (votes + 1e-5) / np.maximum(votes.sum(axis=1, keepdims=True), 1e-5)
    log_emission = np.log(probabilities)
    switch_penalty = 1.65
    score = np.full((len(votes), 2), -np.inf, dtype=np.float64)
    back = np.zeros((len(votes), 2), dtype=np.int8)
    score[0] = log_emission[0]
    for index in range(1, len(votes)):
        # 电话话轮切换通常伴随可见停顿；连续词内部几乎禁止切换，避免“13｜路”、
        # “公交｜公司”这类声纹滑窗边界造成的断词。
        gap = max(0.0, segments[index].start - segments[index - 1].end)
        if gap < 0.08:
            penalty = 5.5
        elif gap < 0.22:
            penalty = 3.2
        elif gap > 0.7:
            penalty = 0.75
        else:
            penalty = switch_penalty
        for label in (0, 1):
            candidates = (score[index - 1, label], score[index - 1, 1 - label] - penalty)
            previous = int(np.argmax(candidates))
            back[index, label] = label if previous == 0 else 1 - label
            score[index, label] = candidates[previous] + log_emission[index, label]
    labels = [0] * len(votes)
    labels[-1] = int(np.argmax(score[-1]))
    for index in range(len(votes) - 1, 0, -1):
        labels[index - 1] = int(back[index, labels[index]])

    # 消除夹在同一说话人之间、持续不足 0.8 秒的孤岛。
    changed = True
    while changed:
        changed = False
        runs: list[tuple[int, int, int]] = []
        begin = 0
        for index in range(1, len(labels) + 1):
            if index == len(labels) or labels[index] != labels[begin]:
                runs.append((begin, index, labels[begin]))
                begin = index
        for run_index in range(1, len(runs) - 1):
            begin, end, label = runs[run_index]
            duration = segments[end - 1].end - segments[begin].start
            if duration < 0.8 and runs[run_index - 1][2] == runs[run_index + 1][2]:
                labels[begin:end] = [1 - label] * (end - begin)
                changed = True
                break
    return labels


def _embedding_windows(
    samples: np.ndarray, sample_rate: int, segments: list[TimedText], campplus_model: Path
) -> tuple[np.ndarray, np.ndarray, bool]:
    """提取 1.8 秒滑窗声纹；CAM++ 不可用时回退手工声学特征。"""
    window_seconds, step_seconds = 1.8, 0.65
    half = window_seconds / 2.0
    start = max(half, segments[0].start)
    stop = min(len(samples) / sample_rate - half, segments[-1].end)
    centers = np.arange(start, stop + 1e-6, step_seconds, dtype=np.float32)
    speech_ranges = [(item.start, item.end) for item in segments]
    kept_centers: list[float] = []
    clips: list[np.ndarray] = []
    for center in centers:
        left, right = float(center - half), float(center + half)
        overlap = sum(max(0.0, min(right, end) - max(left, begin)) for begin, end in speech_ranges)
        if overlap < 0.55:
            continue
        clip = samples[int(left * sample_rate):int(right * sample_rate)]
        if len(clip) < int(window_seconds * sample_rate * 0.95):
            continue
        clips.append(clip)
        kept_centers.append(float(center))

    if campplus_model.exists() and clips:
        features = np.stack([_campplus_fbank(clip, sample_rate) for clip in clips])
        session = _load_campplus(str(campplus_model))
        embeddings = session.run(None, {session.get_inputs()[0].name: features.astype(np.float32)})[0]
        embeddings = embeddings / np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9)
        return embeddings.astype(np.float32), np.asarray(kept_centers), True

    from sklearn.preprocessing import StandardScaler
    fallback_vectors = [_speaker_feature(clip, sample_rate) for clip in clips]
    valid = [(center, feature) for center, feature in zip(kept_centers, fallback_vectors, strict=True) if feature is not None]
    if not valid:
        return np.empty((0, 1), dtype=np.float32), np.empty(0), False
    fallback_centers, vectors = zip(*valid, strict=True)
    return StandardScaler().fit_transform(np.vstack(vectors)), np.asarray(fallback_centers), False


def _resolve_model_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    paths = ((Path.cwd() / candidate).resolve(), (PROJECT_ROOT / candidate).resolve())
    return next((item for item in paths if item.exists()), paths[1])


@lru_cache(maxsize=1)
def _load_campplus(path: str):
    import onnxruntime as ort

    return ort.InferenceSession(path, providers=["CPUExecutionProvider"])


def _campplus_fbank(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """生成接近 Kaldi 配置的 80 维 log-mel 输入。"""
    waveform = samples.astype(np.float32, copy=False) * 32768.0
    waveform = np.append(waveform[0], waveform[1:] - 0.97 * waveform[:-1])
    frame_length, hop, n_fft = int(0.025 * sample_rate), int(0.010 * sample_rate), 512
    frames = np.lib.stride_tricks.sliding_window_view(waveform, frame_length)[::hop]
    window = np.hanning(frame_length).astype(np.float32) ** 0.85
    power = np.abs(np.fft.rfft(frames * window, n=n_fft, axis=1)) ** 2
    mel = _mel_filterbank(sample_rate, n_fft, 80)
    features = np.log(np.maximum(power @ mel.T, 1e-10)).astype(np.float32)
    return features - features.mean(axis=0, keepdims=True)


def _decode_mono(audio_path: str | Path, target_rate: int = 16000) -> tuple[np.ndarray, int]:
    import av

    container = av.open(str(audio_path))
    resampler = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=target_rate)
    chunks: list[np.ndarray] = []
    for frame in container.decode(audio=0):
        converted = resampler.resample(frame)
        if not isinstance(converted, list):
            converted = [converted]
        for output in converted:
            if output is not None:
                chunks.append(output.to_ndarray().reshape(-1).astype(np.float32, copy=False))
    container.close()
    if not chunks:
        raise ValueError("音频解码结果为空")
    samples = np.concatenate(chunks)
    peak = float(np.max(np.abs(samples)))
    if peak > 1.5:  # 整型音频的兼容归一化
        samples = samples / max(peak, 1.0)
    return samples, target_rate


def _speaker_feature(samples: np.ndarray, sample_rate: int) -> np.ndarray | None:
    if len(samples) < int(sample_rate * 0.5):
        return None
    samples = samples.astype(np.float32, copy=False)
    samples = samples - float(np.mean(samples))
    samples = np.append(samples[0], samples[1:] - 0.97 * samples[:-1])
    frame_length = int(0.025 * sample_rate)
    hop = int(0.010 * sample_rate)
    if len(samples) < frame_length:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(samples, frame_length)[::hop]
    if len(frames) > 500:
        frames = frames[np.linspace(0, len(frames) - 1, 500, dtype=int)]
    frames = frames * np.hamming(frame_length)
    energy = np.mean(frames * frames, axis=1)
    voiced = frames[energy > max(float(np.percentile(energy, 30)), 1e-8)]
    if len(voiced) < 5:
        return None

    n_fft = 512
    power = np.abs(np.fft.rfft(voiced, n=n_fft, axis=1)) ** 2
    filterbank = _mel_filterbank(sample_rate, n_fft, 24)
    log_mel = np.log(np.maximum(power @ filterbank.T, 1e-10))
    mfcc = dct(log_mel, type=2, axis=1, norm="ortho")[:, 1:14]
    delta = np.diff(mfcc, axis=0) if len(mfcc) > 1 else np.zeros_like(mfcc)

    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    centroid = (power * freqs).sum(axis=1) / np.maximum(power.sum(axis=1), 1e-9)
    pitches, voicing = _pitch_statistics(voiced, sample_rate)
    return np.concatenate((
        mfcc.mean(axis=0), mfcc.std(axis=0),
        delta.mean(axis=0), delta.std(axis=0),
        np.array([
            float(np.mean(centroid) / 4000.0), float(np.std(centroid) / 4000.0),
            pitches[0] / 300.0, pitches[1] / 300.0, voicing,
            float(np.mean(np.log(np.maximum(energy, 1e-10)))),
        ], dtype=np.float32),
    )).astype(np.float32)


def _pitch_statistics(frames: np.ndarray, sample_rate: int) -> tuple[tuple[float, float], float]:
    min_lag = max(1, int(sample_rate / 350))
    max_lag = min(frames.shape[1] - 1, int(sample_rate / 70))
    values: list[float] = []
    ratios: list[float] = []
    for frame in frames[::max(1, len(frames) // 80)]:
        corr = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
        if corr[0] <= 1e-9:
            continue
        lag = min_lag + int(np.argmax(corr[min_lag:max_lag + 1]))
        ratio = float(corr[lag] / corr[0])
        if ratio >= 0.22:
            values.append(sample_rate / lag)
            ratios.append(ratio)
    if not values:
        return (0.0, 0.0), 0.0
    return (float(np.median(values)), float(np.std(values))), float(np.mean(ratios))


def _mel_filterbank(sample_rate: int, n_fft: int, count: int) -> np.ndarray:
    low_mel = 2595.0 * np.log10(1.0)
    high_mel = 2595.0 * np.log10(1.0 + (sample_rate / 2.0) / 700.0)
    hz = 700.0 * (10 ** (np.linspace(low_mel, high_mel, count + 2) / 2595.0) - 1.0)
    bins = np.floor((n_fft + 1) * hz / sample_rate).astype(int)
    bank = np.zeros((count, n_fft // 2 + 1), dtype=np.float32)
    for index in range(1, count + 1):
        left, center, right = bins[index - 1:index + 2]
        center = max(center, left + 1)
        right = max(right, center + 1)
        for freq_bin in range(left, min(center, bank.shape[1])):
            bank[index - 1, freq_bin] = (freq_bin - left) / (center - left)
        for freq_bin in range(center, min(right, bank.shape[1])):
            bank[index - 1, freq_bin] = (right - freq_bin) / (right - center)
    return bank
