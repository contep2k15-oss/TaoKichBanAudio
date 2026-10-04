"""python tests/test_tts_stagger.py — tts_stagger_sec: nghỉ ĐÚNG trước mỗi lệnh gọi TTS thật (không tính đoạn
đã cache), tts_concurrency giới hạn ĐÚNG số lệnh gọi đồng thời tối đa."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pydub import AudioSegment  # noqa: E402
from pydub.generators import Sine  # noqa: E402

from config import Config  # noqa: E402
from engines.base import BaseTTSEngine, Prosody  # noqa: E402
from script_generator import ScriptSegment  # noqa: E402
from tts_engine import synthesize_all  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


class TrackingEngine(BaseTTSEngine):
    name = "tracking"

    def __init__(self):
        self.call_times: list[float] = []
        self.concurrent_now = 0
        self.max_concurrent = 0

    def default_voice(self, language: str) -> str:
        return "vi-VN-HoaiMyNeural"

    async def synthesize(self, text: str, voice: str, prosody: Prosody, out_path: Path, *,
                         timeout: float, retries: int) -> None:
        self.call_times.append(time.monotonic())
        self.concurrent_now += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent_now)
        await asyncio.sleep(0.05)   # mô phỏng độ trễ mạng thật
        tone = Sine(300).to_audio_segment(duration=1200).apply_gain(-8)
        (AudioSegment.silent(100) + tone).export(out_path, format="mp3")
        self.concurrent_now -= 1


def mk_segments(n):
    return [ScriptSegment(i, i * 2.0, i * 2.0 + 1.5, f"Câu số {i}.", "vui ve") for i in range(1, n + 1)]


async def run(cfg, segs, engine):
    return await synthesize_all(segs, cfg, boundary_sec=100.0, tts_engine=engine)


import tempfile  # noqa: E402

# ── 1) tts_stagger_sec=0 (tắt nghỉ) → các lệnh gọi KHÔNG bị dãn ra đáng kể ──
with tempfile.TemporaryDirectory() as d:
    cfg0 = Config(video_path=None, output_dir=Path(d), tts_concurrency=4, tts_stagger_sec=0.0)
    engine0 = TrackingEngine()
    asyncio.run(run(cfg0, mk_segments(8), engine0))
    gaps0 = [engine0.call_times[i + 1] - engine0.call_times[i] for i in range(len(engine0.call_times) - 1)]
    check(f"stagger=0: khoảng cách trung bình giữa các lệnh gọi RẤT NHỎ (gần như đồng thời): {sum(gaps0)/len(gaps0):.3f}s",
          sum(gaps0) / len(gaps0) < 0.1)

# ── 2) tts_stagger_sec=0.2 → các lệnh gọi bị DÃN RA đúng khoảng đó ──
with tempfile.TemporaryDirectory() as d:
    cfg1 = Config(video_path=None, output_dir=Path(d), tts_concurrency=4, tts_stagger_sec=0.2)
    engine1 = TrackingEngine()
    t0 = time.monotonic()
    asyncio.run(run(cfg1, mk_segments(8), engine1))
    total = time.monotonic() - t0
    check(f"stagger=0.2: tổng thời gian chạy DÀI HƠN hẳn do có nghỉ (8 đoạn, concurrency 4 → ít nhất 2 đợt × 0.2s): {total:.2f}s",
          total >= 0.35)

# ── 3) tts_concurrency=1 → KHÔNG BAO GIỜ có 2 lệnh gọi chạy đồng thời ──
with tempfile.TemporaryDirectory() as d:
    cfg2 = Config(video_path=None, output_dir=Path(d), tts_concurrency=1, tts_stagger_sec=0.0)
    engine2 = TrackingEngine()
    asyncio.run(run(cfg2, mk_segments(6), engine2))
    check(f"concurrency=1: tối đa CHỈ 1 lệnh gọi chạy cùng lúc (thực tế: {engine2.max_concurrent})",
          engine2.max_concurrent == 1)

# ── 4) tts_concurrency=4 → CÓ lúc chạy đồng thời tới 4 (đúng giới hạn, không vượt) ──
with tempfile.TemporaryDirectory() as d:
    cfg3 = Config(video_path=None, output_dir=Path(d), tts_concurrency=4, tts_stagger_sec=0.0)
    engine3 = TrackingEngine()
    asyncio.run(run(cfg3, mk_segments(10), engine3))
    check(f"concurrency=4: có lúc chạy đồng thời ĐÚNG 4 (không vượt giới hạn): {engine3.max_concurrent}",
          engine3.max_concurrent == 4)

# ── 5) đoạn đã có sẵn trong cache KHÔNG bị tính vào stagger (không gọi synthesize() lại) ──
with tempfile.TemporaryDirectory() as d:
    cfg4 = Config(video_path=None, output_dir=Path(d), tts_concurrency=4, tts_stagger_sec=0.2)
    cfg4.tts_dir.mkdir(parents=True, exist_ok=True)
    segs4 = mk_segments(1)
    # tính đúng key cache mà worker() sẽ dùng, tạo sẵn file đó → mô phỏng "đã tổng hợp từ trước"
    import hashlib
    from tts_engine import TONE_PROFILES, clean_tts_text
    from script_generator import normalize_tone
    text = clean_tts_text(segs4[0].text)
    voice = cfg4.voice or "vi-VN-HoaiMyNeural"
    prosody = TONE_PROFILES[normalize_tone(segs4[0].tone)]
    key = hashlib.md5(f"{text}|{voice}|tracking|{prosody.rate}{prosody.pitch}{prosody.volume}".encode()).hexdigest()[:8]
    cache_path = cfg4.tts_dir / f"seg_{segs4[0].id:04d}_{key}.mp3"
    tone = Sine(300).to_audio_segment(duration=1200).apply_gain(-8)
    (AudioSegment.silent(100) + tone).export(cache_path, format="mp3")
    engine4 = TrackingEngine()
    t0 = time.monotonic()
    asyncio.run(run(cfg4, segs4, engine4))
    elapsed = time.monotonic() - t0
    check(f"đoạn đã cache: KHÔNG gọi synthesize() thật: {len(engine4.call_times)} lần gọi", len(engine4.call_times) == 0)
    check(f"đoạn đã cache: KHÔNG bị nghỉ stagger (chạy nhanh): {elapsed:.2f}s", elapsed < 0.15)

print("TẤT CẢ PASS ✔")
