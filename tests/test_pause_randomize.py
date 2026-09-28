"""python tests/test_pause_randomize.py — lời đọc PHẢI khớp mốc kịch bản; nghỉ ngẫu nhiên chỉ trong giới hạn, KHÔNG cộng dồn."""
import random
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging  # noqa: E402

from pydub.generators import Sine  # noqa: E402

from audio_assembler import assemble_voiceover_track  # noqa: E402
from config import Config  # noqa: E402
from tts_engine import TTSResult  # noqa: E402

logging.disable(logging.CRITICAL)


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def mk(tmp: Path, sid: int, start_sec: float, audio_ms: int, planned: float) -> TTSResult:
    p = tmp / f"s{audio_ms}.wav"
    if not p.exists():
        Sine(300).to_audio_segment(duration=audio_ms).apply_gain(-6).export(p, format="wav")
    r = TTSResult(id=sid, text="x", start_sec=start_sec, planned_sec=planned, available_sec=planned + 1)
    r.fitted_path, r.raw_sec, r.final_sec, r.status = p, audio_ms / 1000, audio_ms / 1000, "ok"
    return r


def run(tmp, results, total_ms, seed=1, **cfgkw):
    cfg = Config(video_path=None, output_dir=tmp / "o", **cfgkw)
    return assemble_voiceover_track(results, total_ms, cfg, out_path=tmp / "v.mp3", report_path=tmp / "r.json",
                                    rng=random.Random(seed))[1]


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    # Kịch bản TÁI HIỆN LỖI THẬT: 60 câu cách nhau 4,5s, khung 3,5s, giọng đọc ~2s (điển hình) → luôn có khoảng trống lớn
    results = [mk(tmp, i + 1, 2.0 + i * 4.5, 2000, 3.5) for i in range(60)]
    total_ms = int((2.0 + 60 * 4.5 + 5) * 1000)

    # ── 1) MẶC ĐỊNH: khớp tuyệt đối mốc, KHÔNG lệch dù bao nhiêu câu ──
    pl = run(tmp, results, total_ms)
    check("MẶC ĐỊNH (max_advance=0): cả 60 câu đặt ĐÚNG mốc kịch bản, lệch 0ms", all(p.start_drift_ms == 0 for p in pl))
    check(f"câu thứ 60 vẫn đúng mốc {pl[-1].planned_start_ms/1000:.1f}s (bản cũ: lệch sớm 104 giây)",
          pl[-1].placed_start_ms == pl[-1].planned_start_ms)

    # ── 2) CHO PHÉP lệch tối đa 0.4s: mọi câu lệch ≤0.4s, KHÔNG cộng dồn theo độ dài ──
    pl = run(tmp, results, total_ms, max_advance_sec=0.4)
    drifts = [-p.start_drift_ms for p in pl]                  # dương = sớm hơn mốc
    check(f"max_advance=0.4s: MỌI câu lệch ≤400ms (lớn nhất {max(drifts)}ms)", all(0 <= x <= 400 for x in drifts))
    check("độ lệch KHÔNG cộng dồn: trung bình 15 câu cuối ≈ 15 câu đầu (không tăng theo thời gian)",
          abs(statistics.mean(drifts[-15:]) - statistics.mean(drifts[1:16])) < 150)
    check("có ngẫu nhiên thật (nhiều giá trị lệch khác nhau, không phải hằng số)", len(set(drifts)) > 15)
    check("không câu nào chồng lên câu trước", all(a.placed_end_ms <= b.placed_start_ms for a, b in zip(pl, pl[1:])))
    check("câu đầu tiên không bị dịch", pl[0].start_drift_ms == 0)

    # ── 3) trần lớn hơn thì lệch nhiều hơn nhưng VẪN bị chặn ở trần (không vượt) ──
    for cap in (0.2, 1.0, 2.0):
        pl = run(tmp, results, total_ms, max_advance_sec=cap)
        worst = max(-p.start_drift_ms for p in pl)
        check(f"max_advance={cap}s: lệch lớn nhất {worst}ms ≤ {int(cap * 1000)}ms", worst <= int(cap * 1000))

    # ── 4) nghỉ ngẫu nhiên: khi cho phép đủ rộng, khoảng nghỉ thực tế nằm trong [min,max] ──
    gaps = []
    for seed in range(60):
        ra = mk(tmp, 1, 0.0, 500, 1.0); rb = mk(tmp, 2, 5.0, 500, 1.0)
        pl = run(tmp, [ra, rb], 10000, seed=seed, max_advance_sec=5.0, min_pause_sec=0.3, max_pause_sec=1.2)
        gaps.append(pl[1].placed_start_ms - pl[0].placed_end_ms)
    check(f"cho phép rộng: 60 lần đều có khoảng nghỉ trong [300,1200]ms (min {min(gaps)}, max {max(gaps)})", all(300 <= g <= 1200 for g in gaps))
    check(f"khoảng nghỉ dao động thật ({len(set(gaps))} giá trị khác nhau/60 lần)", len(set(gaps)) > 20)

    # ── 5) không bao giờ bịa thêm khoảng lặng: nghỉ tối thiểu lớn hơn phần dư sẵn có → giữ nguyên ──
    ra = mk(tmp, 1, 0.0, 500, 1.0); rb = mk(tmp, 2, 1.3, 500, 1.0)          # phần dư tự nhiên chỉ 800ms
    pl = run(tmp, [ra, rb], 3000, max_advance_sec=5.0, min_pause_sec=5.0, max_pause_sec=10.0)
    check("nghỉ tối thiểu > phần dư sẵn có → câu KHÔNG bị dịch", pl[1].start_drift_ms == 0)

    # ── 6) báo cáo có độ lệch lớn nhất; không báo lệch giả khi đang trong phép ──
    import json
    pl = run(tmp, results, total_ms, max_advance_sec=0.4)
    rep = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
    check("báo cáo có max_abs_drift_ms và allowed_advance_ms",
          rep["summary"]["max_abs_drift_ms"] <= 400 and rep["summary"]["allowed_advance_ms"] == 400)
    check("không cảnh báo 'lệch không giải thích được' khi độ lệch nằm trong phép", rep["summary"]["start_drift_over_tolerance"] == 0)

    # ── 7) validation ──
    for bad in (-0.1, 5.1):
        try:
            Config(video_path=None, output_dir=tmp / "o", max_advance_sec=bad)
            raise AssertionError("phải từ chối")
        except ValueError:
            pass
    print("  ✓ max_advance_sec ngoài [0,5] bị từ chối")

print("TẤT CẢ PASS ✔")
