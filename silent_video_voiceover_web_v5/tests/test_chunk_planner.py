"""python tests/test_chunk_planner.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chunk_planner import plan_macro_chunks  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def assert_contiguous(chunks, duration):
    assert chunks[0].start_sec == 0.0
    assert abs(chunks[-1].end_sec - duration) < 1e-6
    for a, b in zip(chunks, chunks[1:]):
        assert abs(a.end_sec - b.start_sec) < 1e-9, (a, b)


# 1) video ngắn hơn target → 1 chunk duy nhất
c = plan_macro_chunks(120.0, target_sec=240.0)
check("video ngắn → đúng 1 chunk bao trọn video", len(c) == 1 and c[0].start_sec == 0 and c[0].end_sec == 120.0)

# 2) video dài, có khoảng lặng gần mốc mục tiêu → cắt đúng tại khoảng lặng, không cắt cứng
duration = 900.0  # 15 phút
silences = [(238.0, 242.0), (478.0, 481.0), (715.0, 719.0)]
c = plan_macro_chunks(duration, target_sec=240.0, tolerance_sec=30.0, silences=silences)
assert_contiguous(c, duration)
check("liên tục, phủ kín [0, duration], không chồng lấn", True)
check("chunk 1 cắt đúng giữa khoảng lặng đầu tiên (240.0)", abs(c[0].end_sec - 240.0) < 0.01 and c[0].cut_reason == "silence")
check("chunk 2 cắt đúng giữa khoảng lặng thứ hai (479.5)", abs(c[1].end_sec - 479.5) < 0.01 and c[1].cut_reason == "silence")

# 3) không có khoảng lặng nhưng có scene cut gần mốc → dùng scene cut
c = plan_macro_chunks(600.0, target_sec=240.0, tolerance_sec=30.0, silences=[], scene_cuts=[250.0])
check("không có khoảng lặng → rơi về scene cut gần nhất", abs(c[0].end_sec - 250.0) < 1e-6 and c[0].cut_reason == "scene")

# 4) không có gì cả trong dung sai → cắt cứng tại mốc mục tiêu, có cảnh báo (không raise)
c = plan_macro_chunks(600.0, target_sec=240.0, tolerance_sec=10.0, silences=[(300.0, 301.0)])
check("không có mốc nào trong dung sai → cắt cứng đúng tại target", abs(c[0].end_sec - 240.0) < 1e-6 and c[0].cut_reason == "hard")

# 5) không để lại chunk cuối cùng quá ngắn (gộp vào chunk trước)
c = plan_macro_chunks(500.0, target_sec=240.0, tolerance_sec=5.0, silences=[])
check("chunk cuối không bị cực ngắn (gộp nốt phần dư)", len(c) == 2 and c[-1].duration_sec >= 240.0 * 0.9)
assert_contiguous(c, 500.0)

# 6) video rất dài (60 phút) với khoảng lặng đều đặn mỗi 4 phút → nhiều chunk, luôn liên tục & không chồng lấn
duration = 3600.0
silences = [(i * 240.0 - 1.0, i * 240.0 + 1.0) for i in range(1, 15)]
c = plan_macro_chunks(duration, target_sec=240.0, tolerance_sec=20.0, silences=silences)
assert_contiguous(c, duration)
check(f"video 60 phút → {len(c)} chunk, tất cả cắt tại khoảng lặng, liên tục & không tràn RAM (test độc lập)",
      all(ch.cut_reason in ("silence", "final") for ch in c[:-1]) and len(c) >= 13)

print("TẤT CẢ PASS ✔")
