"""python tests/test_video_retime.py — dùng FFmpeg thật, video tổng hợp ngắn."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import probe_media  # noqa: E402
from video_retime import RetimeWindow, build_retimed_video, build_time_remap, total_new_duration  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def make_video(path: Path, duration: float = 10.0) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc2=size=320x180:rate=25:duration={duration}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


# 1) build_time_remap: đoạn 4s-6s làm chậm 2x (dài ra thành 4s) -> mọi mốc SAU 6s bị dịch +2s
remap = build_time_remap(10.0, [RetimeWindow(4.0, 6.0, 2.0)])
check("trước cửa sổ retime: không đổi", abs(remap(2.0) - 2.0) < 1e-6)
check("giữa cửa sổ retime: giãn tuyến tính (t=5 -> 4 + 1*2 = 6)", abs(remap(5.0) - 6.0) < 1e-6)
check("đúng cuối cửa sổ: 6 -> 4 + 2*2 = 8", abs(remap(6.0) - 8.0) < 1e-6)
check("sau cửa sổ retime: dịch nguyên +2s (t=8 -> 10)", abs(remap(8.0) - 10.0) < 1e-6)
check("total_new_duration = 10 + 2*(2-1) = 12", abs(total_new_duration(10.0, [RetimeWindow(4.0, 6.0, 2.0)]) - 12.0) < 1e-6)

# 2) hai cửa sổ, không chồng lấn
remap2 = build_time_remap(20.0, [RetimeWindow(2.0, 4.0, 1.5), RetimeWindow(10.0, 11.0, 3.0)])
# [0,2) giữ nguyên; [2,4) *1.5 -> dài 3s (mới: 2..5); [4,10) giữ nguyên dịch +1s (mới: 5..11);
# [10,11) *3 -> dài 3s (mới: 11..14); [11,20) dịch +1+2=+3s (mới: 14..23)
check("mốc giữa 2 cửa sổ (t=7) dịch đúng theo cửa sổ 1 (+1s)", abs(remap2(7.0) - 8.0) < 1e-6)
check("mốc sau cả 2 cửa sổ (t=15) dịch đúng tổng (+1+2=+3s)", abs(remap2(15.0) - 18.0) < 1e-6)

# 3) validate: factor <= 1.0 bị từ chối ngay khi tạo RetimeWindow
try:
    RetimeWindow(1.0, 2.0, 1.0)
    raise AssertionError("phải ValueError")
except ValueError:
    print("  ✓ RetimeWindow từ chối factor ≤ 1.0")

# 4) build_retimed_video THẬT bằng FFmpeg: video 10s, làm chậm 2x đoạn [4,6) -> tổng dài 12s
with tempfile.TemporaryDirectory() as d:
    t = Path(d)
    src = t / "src.mp4"
    make_video(src, 10.0)
    out = t / "retimed.mp4"
    build_retimed_video(src, 10.0, [RetimeWindow(4.0, 6.0, 2.0)], out)
    info = probe_media(out)
    check(f"video retime thật: 10s -> {info.duration_sec:.2f}s (kỳ vọng ~12s)", abs(info.duration_sec - 12.0) < 0.3)

    # nhiều cửa sổ cùng lúc, một lệnh FFmpeg duy nhất (một input, không WinError-206-style)
    out2 = t / "retimed2.mp4"
    build_retimed_video(src, 10.0, [RetimeWindow(1.0, 2.0, 1.5), RetimeWindow(7.0, 8.0, 1.5)], out2)
    info2 = probe_media(out2)
    check(f"nhiều cửa sổ: 10s -> {info2.duration_sec:.2f}s (kỳ vọng ~11s)", abs(info2.duration_sec - 11.0) < 0.3)

print("TẤT CẢ PASS ✔")
