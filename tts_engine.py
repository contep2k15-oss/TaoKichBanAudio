"""BƯỚC 3 — tts_engine: Voiceover Generation & Time-Fitting.

Mỗi đoạn kịch bản → audio (qua `BaseTTSEngine` — Edge-TTS mặc định, có thể đổi sang ElevenLabs, xem
`engines/`) → cắt khoảng lặng đầu/cuối → đo `actual_duration` → `time_fit.compute_fit()` quyết định có
cần tăng tốc (FFmpeg `atempo`, chất lượng tốt hơn `pydub.speedup` vì không đổi cao độ) hay không, tối đa
`max_speedup` (mặc định 1.25x). Kết quả từng đoạn là WAV đã "vừa khung hình", sẵn sàng cho Bước 4.

Module này KHÔNG biết đang dùng Edge-TTS hay ElevenLabs — chỉ gọi qua `BaseTTSEngine.synthesize()`
(dependency injection qua tham số `tts_engine` của `synthesize_all()`), nên toàn bộ logic time-fit/atempo
dưới đây dùng chung cho MỌI engine TTS.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from config import Config
from engines.base import BaseTTSEngine, Prosody
from script_generator import ScriptSegment, normalize_tone
from time_fit import compute_fit
from utils import TTSError, run_cmd

log = logging.getLogger("svvc.tts")

# tone -> thông số prosody trung lập (mỗi engine tự diễn dịch theo khả năng của mình, xem engines/tts_*.py)
TONE_PROFILES: dict[str, Prosody] = {
    "vui ve":     Prosody(rate="+5%",  pitch="+3Hz", volume="+0%"),
    "hao hung":   Prosody(rate="+10%", pitch="+6Hz", volume="+5%"),
    "tram am":    Prosody(rate="-8%",  pitch="-4Hz", volume="-3%"),
    "trung tinh": Prosody(rate="+0%",  pitch="+0Hz", volume="+0%"),
}
SILENCE_THRESH_DBFS = -45
KEEP_SILENCE_MS = 60


@dataclass
class TTSResult:
    id: int
    text: str
    start_sec: float
    planned_sec: float          # duration_sec trong kịch bản ("thời lượng bối cảnh gốc")
    available_sec: float        # tối đa được phép chiếm tới đầu đoạn kế tiếp (cho phép gap-fill)
    raw_path: Path | None = None
    fitted_path: Path | None = None
    raw_sec: float = 0.0        # actual_duration sau khi cắt lặng - chính là "thời lượng TTS" trong công thức r
    final_sec: float = 0.0
    ratio: float = 1.0          # r = actual_duration / target_duration (đúng công thức đề bài)
    speed: float = 1.0
    status: str = "pending"     # ok | sped_up | overflow | clipped | needs_video_retime | failed | skipped
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["raw_path"], d["fitted_path"] = str(self.raw_path or ""), str(self.fitted_path or "")
        return d


def clean_tts_text(text: str) -> str:
    text = re.sub(r"[*_`#<>~]+", "", text)
    return " ".join(text.split())


# ════════════════════════════════════════════════════════════
# Xử lý audio (blocking -> chạy trong thread, không chặn event loop khi TTS engine khác đang chạy song song)
# ════════════════════════════════════════════════════════════
def _trim_silence(audio):
    from pydub.silence import detect_leading_silence
    lead = detect_leading_silence(audio, silence_threshold=SILENCE_THRESH_DBFS)
    tail = detect_leading_silence(audio.reverse(), silence_threshold=SILENCE_THRESH_DBFS)
    start, end = max(0, lead - KEEP_SILENCE_MS), len(audio) - max(0, tail - KEEP_SILENCE_MS)
    trimmed = audio[start:end]
    return trimmed if len(trimmed) >= 100 else audio


def _fit_audio(res: TTSResult, cfg: Config) -> None:
    """Cắt lặng -> đo actual_duration -> time_fit.compute_fit() -> (nếu cần) atempo. Cập nhật `res` tại chỗ."""
    from pydub import AudioSegment

    audio = AudioSegment.from_file(res.raw_path)
    if cfg.trim_silence:
        audio = _trim_silence(audio)
    audio = audio.set_channels(1).set_sample_width(2)
    res.raw_sec = len(audio) / 1000.0

    plan = compute_fit(res.raw_sec, res.planned_sec, res.available_sec, max_speedup=cfg.max_speedup,
                       allow_video_retime=cfg.allow_video_retime)
    res.ratio, res.speed, res.status = plan.ratio, plan.speed, plan.status

    stem = res.raw_path.with_suffix("")
    trimmed_wav = Path(f"{stem}_trim.wav")
    fitted_wav = Path(f"{stem}_fit.wav")
    audio.export(trimmed_wav, format="wav")
    del audio                                            # giải phóng mảng PCM ngay sau khi export ra đĩa

    if plan.speed > 1.001:
        run_cmd(["ffmpeg", "-y", "-loglevel", "error", "-i", trimmed_wav, "-filter:a", f"atempo={plan.speed:.5f}",
                 "-c:a", "pcm_s16le", fitted_wav], f"atempo đoạn {res.id}", timeout=120)
        trimmed_wav.unlink(missing_ok=True)
        res.final_sec = len(AudioSegment.from_wav(fitted_wav)) / 1000.0
    else:
        trimmed_wav.replace(fitted_wav)
        res.final_sec = res.raw_sec
    res.fitted_path = fitted_wav


# ════════════════════════════════════════════════════════════
# Điểm vào
# ════════════════════════════════════════════════════════════
async def synthesize_all(segments: list[ScriptSegment], cfg: Config, boundary_sec: float, *,
                         tts_engine: BaseTTSEngine, on_progress: Callable[[int, int], None] | None = None
                         ) -> list[TTSResult]:
    """`boundary_sec`: mốc thời gian không được lấn qua (hết video, hoặc hết MACRO-CHUNK hiện tại khi xử
    lý video dài theo từng chunk - xem long_video_pipeline.py)."""
    cfg.tts_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(segments, key=lambda s: s.start_sec)
    results: list[TTSResult] = []
    for i, seg in enumerate(ordered):
        nxt = ordered[i + 1].start_sec if i + 1 < len(ordered) else boundary_sec
        results.append(TTSResult(id=seg.id, text=clean_tts_text(seg.text), start_sec=seg.start_sec,
                                 planned_sec=seg.duration_sec, available_sec=max(0.0, nxt - seg.start_sec)))

    sem = asyncio.Semaphore(max(1, cfg.tts_concurrency))
    done = 0
    total = len(results)

    async def worker(seg: ScriptSegment, res: TTSResult) -> None:
        nonlocal done
        try:
            if not res.text:
                res.status = "skipped"
                return
            prosody = TONE_PROFILES[normalize_tone(seg.tone)]
            voice = cfg.voice or tts_engine.default_voice(cfg.language)
            key = hashlib.md5(f"{res.text}|{voice}|{tts_engine.name}|{prosody.rate}{prosody.pitch}{prosody.volume}"
                              .encode()).hexdigest()[:8]
            res.raw_path = cfg.tts_dir / f"seg_{seg.id:04d}_{key}.mp3"
            if not (res.raw_path.is_file() and res.raw_path.stat().st_size > 0):   # cache theo nội dung+engine+voice
                async with sem:
                    await tts_engine.synthesize(res.text, voice, prosody, res.raw_path,
                                                timeout=cfg.tts_timeout_sec, retries=cfg.tts_retries)
            await asyncio.to_thread(_fit_audio, res, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 1 đoạn lỗi không được làm hỏng cả pipeline
            res.status, res.error = "failed", f"{type(e).__name__}: {e}"
            log.error("Đoạn %d thất bại: %s", res.id, res.error)
        finally:
            done += 1
            if on_progress:
                on_progress(done, total)
            if done % 10 == 0 or done == total:
                log.info("TTS tiến độ: %d/%d", done, total)

    await asyncio.gather(*(worker(seg, res) for seg, res in zip(ordered, results)))
    _report(results)
    if all(r.status in ("failed", "skipped") for r in results):
        raise TTSError(f"Không tạo được audio cho bất kỳ đoạn nào (engine: {tts_engine.name}). "
                       "Kiểm tra kết nối mạng / API key / tên voice.")
    return results


def _report(results: list[TTSResult]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info("Kết quả TTS: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for r in results:
        if r.status == "sped_up":
            log.info("  #%d: r=%.2f -> %.2fs -> %.2fs (tăng tốc %.2fx) để vừa %.2fs.", r.id, r.ratio, r.raw_sec,
                     r.final_sec, r.speed, r.planned_sec)
        elif r.status == "overflow":
            log.warning("  #%d: r=%.2f, đã tăng tốc tối đa %.2fx vẫn dài %dms > %dms (kế hoạch) -> lấn sang khoảng "
                        "trống phía sau.", r.id, r.ratio, r.speed, round(r.final_sec * 1000), round(r.planned_sec * 1000))
        elif r.status == "needs_video_retime":
            log.warning("  #%d: r=%.2f, vượt cả khoảng trống -> sẽ nhờ video_retime làm chậm khung hình trong "
                        "cửa sổ này lại.", r.id, r.ratio)
        elif r.status == "clipped":
            log.error("  #%d: r=%.2f, dài %.2fs sau khi tăng tốc tối đa, vượt cả khoảng trống (%.2fs) -> sẽ bị cắt "
                      "ở Bước 4. Hãy rút gọn text hoặc bật --allow-video-retime.", r.id, r.ratio, r.final_sec, r.available_sec)
