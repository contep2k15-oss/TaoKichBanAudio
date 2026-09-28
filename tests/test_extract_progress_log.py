"""python tests/test_extract_progress_log.py — trích frame PHẢI báo tiến độ (không im lặng hàng chục phút)."""
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import video_processor  # noqa: E402
from config import Config  # noqa: E402
from smoke_test import make_video  # noqa: E402
from utils import probe_media  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    video = tmp / "v.mp4"
    make_video(video, 30.0, with_audio=False)
    cfg = Config(video_path=video, output_dir=tmp / "out", frame_interval_sec=1.0)
    cfg.ensure_dirs()
    media = probe_media(video)

    cap = Capture()
    lg = logging.getLogger("svvc.video")
    lg.addHandler(cap)
    lg.setLevel(logging.INFO)
    try:
        samples = video_processor.extract_samples_in_range(cfg, media, 0, 0.0, 30.0, scene_cuts_full_video=[])
    finally:
        lg.removeHandler(cap)

    progress = [l for l in cap.lines if l.startswith("Trích frame")]
    check(f"có dòng tiến độ ({len(progress)} dòng cho {len(samples)} frame)", len(progress) >= 3)
    check("dòng đầu tiên báo 1/N", progress[0].startswith("Trích frame 1/"))
    check("có báo tổng số frame dự kiến đúng", f"/{len(samples)}" in progress[0])
    check("có ước tính thời gian còn lại từ dòng thứ 2", any("còn ~" in l and "s/khung" in l for l in progress[1:]))
    check("KHÔNG log mỗi khung hình (tránh spam) — thưa hơn số frame thật", len(progress) < len(samples))

print("TẤT CẢ PASS ✔")
