"""python tests/test_pause_randomize.py — kiểm thử cơ chế rút ngắn khoảng nghỉ ngẫu nhiên giữa câu."""
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pydub.generators import Sine  # noqa: E402

from audio_assembler import assemble_voiceover_track  # noqa: E402
from config import Config  # noqa: E402
from tts_engine import TTSResult  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def make_result(tmp: Path, sid: int, start_sec: float, dur_ms: int, planned_sec: float, available_sec: float) -> TTSResult:
    p = tmp / f"seg_{sid}_fit.wav"
    Sine(300).to_audio_segment(duration=dur_ms).apply_gain(-6).export(p, format="wav")
    r = TTSResult(id=sid, text="x", start_sec=start_sec, planned_sec=planned_sec, available_sec=available_sec)
    r.fitted_path, r.raw_sec, r.final_sec, r.status = p, dur_ms / 1000, dur_ms / 1000, "ok"
    return r


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg = Config(video_path=None, output_dir=tmp / "out", min_pause_sec=0.3, max_pause_sec=1.2)

    # ── 1) Khoảng trống tự nhiên LỚN (3s) giữa 2 câu 1s audio → phải bị rút ngắn về đúng trong [0.3, 1.2]s ──
    # đoạn 1: 0.0-1.0s (audio 1s) | đoạn 2 theo script: bắt đầu tại 4.0s → khoảng trống tự nhiên = 3.0s
    r1 = make_result(tmp, 1, 0.0, 1000, 1.0, 4.0)
    r2 = make_result(tmp, 2, 4.0, 1000, 1.0, 4.0)
    rng = random.Random(42)
    track, placed = assemble_voiceover_track([r1, r2], 8000, cfg, out_path=tmp / "v1.mp3", report_path=tmp / "r1.json", rng=rng)
    gap_ms = placed[1].placed_start_ms - placed[0].placed_end_ms
    check(f"khoảng nghỉ sau rút ngắn = {gap_ms}ms, nằm đúng trong [300,1200]ms", 300 <= gap_ms <= 1200)
    check("đoạn 2 được kéo SỚM lên so với start_time gốc (start_drift âm)", placed[1].start_drift_ms < 0)
    check("pause_shrink_ms ghi đúng bằng phần đã rút ngắn", placed[1].pause_shrink_ms == 3000 - gap_ms)
    check("KHÔNG bị báo lệch nhịp ngoài dung sai (đã trừ phần rút ngắn chủ ý)",
          abs(placed[1].start_drift_ms + placed[1].pause_shrink_ms) <= 10)

    # ── 2) Khoảng trống tự nhiên NHỎ (0.15s, nhỏ hơn cả min_pause) → giữ nguyên, không đẩy lùi/rút thêm ──
    r3 = make_result(tmp, 3, 0.0, 1000, 1.0, 1.15)
    r4 = make_result(tmp, 4, 1.15, 1000, 1.0, 1.15)
    track2, placed2 = assemble_voiceover_track([r3, r4], 4000, cfg, out_path=tmp / "v2.mp3", report_path=tmp / "r2.json",
                                               rng=random.Random(7))
    check("khoảng trống tự nhiên đã nhỏ hơn min_pause → giữ nguyên (pause_shrink=0)", placed2[1].pause_shrink_ms == 0)
    check("vị trí đặt = đúng start_time gốc (không đổi)", placed2[1].start_drift_ms == 0)

    # ── 3) Ngẫu nhiên thật: chạy 200 lần với seed khác nhau, gap luôn nằm trong [min,max]*1000, có dao động (không phải hằng số) ──
    gaps = []
    for seed in range(200):
        ra = make_result(tmp, 1, 0.0, 500, 1.0, 5.0)
        rb = make_result(tmp, 2, 5.0, 500, 1.0, 5.0)
        _, pl = assemble_voiceover_track([ra, rb], 10000, cfg, out_path=tmp / "vx.mp3", report_path=tmp / "rx.json",
                                         rng=random.Random(seed))
        gaps.append(pl[1].placed_start_ms - pl[0].placed_end_ms)
    check("toàn bộ 200 lần đều nằm trong [300,1200]ms", all(300 <= g <= 1200 for g in gaps))
    check(f"CÓ dao động ngẫu nhiên thật (không phải luôn 1 giá trị cố định) — {len(set(gaps))} giá trị khác nhau/200 lần",
          len(set(gaps)) > 20)
    import statistics
    mean_gap = statistics.mean(gaps)
    check(f"trung bình ~750ms (giữa 300 và 1200) — thực tế: {mean_gap:.0f}ms", 500 < mean_gap < 1000)

    # ── 4) Không bao giờ vượt quá phần dư sẵn có, kể cả khi max_pause_sec đặt rất lớn ──
    cfg_big = Config(video_path=None, output_dir=tmp / "out2", min_pause_sec=5.0, max_pause_sec=10.0)
    r5 = make_result(tmp, 1, 0.0, 500, 1.0, 1.3)
    r6 = make_result(tmp, 2, 1.3, 500, 1.0, 1.3)
    _, placed3 = assemble_voiceover_track([r5, r6], 3000, cfg_big, out_path=tmp / "v3.mp3", report_path=tmp / "r3.json",
                                          rng=random.Random(1))
    gap3 = placed3[1].placed_start_ms - placed3[0].placed_end_ms
    natural = 1300 - 500  # 800ms
    check(f"min_pause=5s > khoảng trống tự nhiên (800ms) → CHỈ dùng tối đa phần dư sẵn có ({gap3}ms ≤ 800ms)", gap3 <= natural)

    # ── 5) Đoạn ĐẦU TIÊN không có 'câu trước' → không áp dụng rút ngắn, giữ nguyên vị trí gốc ──
    r7 = make_result(tmp, 1, 2.0, 500, 1.0, 3.0)
    _, placed4 = assemble_voiceover_track([r7], 5000, cfg, out_path=tmp / "v4.mp3", report_path=tmp / "r4.json",
                                          rng=random.Random(1))
    check("đoạn đầu tiên: pause_shrink=0, vị trí đúng start_time gốc", placed4[0].pause_shrink_ms == 0 and placed4[0].start_drift_ms == 0)

print("TẤT CẢ PASS ✔")
