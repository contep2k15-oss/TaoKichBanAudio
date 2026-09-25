"""python tests/test_time_fit.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from time_fit import compute_fit  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


# 1) audio ngắn hơn/khớp khung cảnh → giữ nguyên tốc độ
p = compute_fit(actual_sec=3.9, target_sec=4.0, available_sec=4.0)
check("audio ngắn hơn khung cảnh → speed=1.0, status=ok", p.speed == 1.0 and p.status == "ok")

# 2) dài hơn nhưng trong tầm 1.25x → sped_up, r = actual/target đúng công thức đề bài
p = compute_fit(actual_sec=5.0, target_sec=4.0, available_sec=4.0)
check("r = actual/target = 1.25", abs(p.ratio - 1.25) < 1e-9)
check("trong tầm max_speedup → sped_up, speed ≈ 1.25x, final_sec ≈ target", p.status == "sped_up" and 1.24 <= p.speed <= 1.26 and abs(p.final_sec - 4.0) < 0.02)

# 3) vượt 1.25x nhưng vẫn còn chỗ trống trước đoạn kế → overflow (lấn khoảng trống), KHÔNG cắt
p = compute_fit(actual_sec=6.0, target_sec=4.0, available_sec=5.0)  # cần speed 1.25 -> final=4.8s, available=5.0s đủ chỗ
check("vượt max_speedup nhưng đủ available → overflow (lấn gap), speed=max", p.status == "overflow" and p.speed == 1.25 and p.final_sec <= 5.0 + 1e-6)

# 4) vượt cả available, KHÔNG cho retime video → clipped (sẽ bị cắt ở audio_assembler)
p = compute_fit(actual_sec=8.0, target_sec=4.0, available_sec=4.2, allow_video_retime=False)
check("vượt cả available, không cho retime → clipped", p.status == "clipped" and p.speed == 1.25)

# 5) vượt cả available, CHO PHÉP retime video → needs_video_retime + tính đúng retime_factor
p = compute_fit(actual_sec=8.0, target_sec=4.0, available_sec=4.2, allow_video_retime=True)
final = 8.0 / 1.25
check("cho phép retime → needs_video_retime", p.status == "needs_video_retime")
check("retime_factor = final_sec / available (video cần dài thêm bấy nhiêu lần)", abs(p.retime_factor - final / 4.2) < 1e-9 and p.retime_factor > 1.0)

# 6) max_speedup tuỳ chỉnh được (vd giới hạn 1.15x thay vì 1.25x mặc định)
p = compute_fit(actual_sec=4.5, target_sec=4.0, available_sec=4.0, max_speedup=1.15)
check("max_speedup tuỳ chỉnh: r=1.125 vẫn ≤ 1.15 → sped_up", p.status == "sped_up" and p.speed <= 1.15 + 0.01)

# 7) không bao giờ LÀM CHẬM giọng đọc (speed luôn ≥ 1.0)
for a, t in [(1.0, 4.0), (0.5, 4.0), (3.9, 4.0)]:
    p = compute_fit(actual_sec=a, target_sec=t, available_sec=t)
    check(f"actual={a}s < target={t}s → speed=1.0 (không kéo giãn giọng đọc)", p.speed == 1.0)

print("TẤT CẢ PASS ✔")
