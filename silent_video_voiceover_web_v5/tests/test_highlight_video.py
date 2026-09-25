"""python tests/test_highlight_video.py — dùng FFmpeg thật, xác nhận cắt+nối đúng khớp thời gian."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from highlight_selector import build_highlight_video  # noqa: E402
from utils import probe_media  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def make_video(path: Path, duration: float = 30.0) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"testsrc2=size=320x180:rate=25:duration={duration}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    src = tmp / "src.mp4"
    make_video(src, 30.0)
    info = probe_media(src)
    check(f"video gốc dài đúng 30s (thực tế {info.duration_sec:.2f}s)", abs(info.duration_sec - 30.0) < 0.2)

    # chọn 3 đoạn KHÔNG liền mạch: [2,7) + [12,15) + [25,30) = 5+3+5 = 13s
    windows = [(2.0, 7.0), (12.0, 15.0), (25.0, 30.0)]
    out = tmp / "highlight.mp4"
    build_highlight_video(src, windows, out, tmp / "work")
    info2 = probe_media(out)
    check(f"video highlight dài đúng tổng 3 đoạn đã chọn: 13s (thực tế {info2.duration_sec:.2f}s)",
          abs(info2.duration_sec - 13.0) < 0.5)
    check("video highlight NGẮN HƠN HẲN bản gốc (30s → ~13s)", info2.duration_sec < info.duration_sec * 0.6)
    check("file trung gian (part_*.mp4, concat_list.txt) đã được dọn sạch sau khi xong",
          not any((tmp / "work" / "highlight_cuts").glob("part_*.mp4"))
          and not (tmp / "work" / "highlight_cuts" / "concat_list.txt").is_file())

    # windows rỗng → báo lỗi rõ ràng, không tạo file rác
    try:
        build_highlight_video(src, [], tmp / "empty.mp4", tmp / "work2")
        raise AssertionError("phải báo lỗi khi windows rỗng")
    except Exception as e:  # noqa: BLE001
        check(f"windows rỗng → báo lỗi rõ ràng: {type(e).__name__}", "PipelineError" in type(e).__name__)

print("TẤT CẢ PASS ✔")
