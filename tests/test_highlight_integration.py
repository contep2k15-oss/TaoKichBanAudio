"""python tests/test_highlight_integration.py — plan_highlight_windows với Gemini giả lập + FFmpeg thật."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gemini_web_driver as gwd  # noqa: E402
from config import Config  # noqa: E402
from engines.vision_gemini_web import GeminiWebVisionEngine  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from highlight_selector import plan_highlight_windows  # noqa: E402
from smoke_test import make_video  # noqa: E402
from utils import format_timestamp, probe_media, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


HOT_ZONE = (18.0, 24.0)   # "đoạn hay" giả lập nằm trong khoảng này của video 30s


def fake_reply(req: dict) -> str:
    import re
    prompt = req["prompt"]
    m = re.search(r"trong khoảng \[(\d\d:\d\d:\d\d\.\d+), (\d\d:\d\d:\d\d\.\d+)\]", prompt)
    from utils import parse_timestamp
    t0, t1 = parse_timestamp(m.group(1)), parse_timestamp(m.group(2))
    overlap = max(0.0, min(t1, HOT_ZONE[1]) - max(t0, HOT_ZONE[0]))
    if overlap > 1.0:   # lô này trùng vùng "hay" giả lập → trả điểm cao
        seg = [{"start_time": format_timestamp(max(t0, HOT_ZONE[0])), "end_time": format_timestamp(min(t1, HOT_ZONE[1])),
               "score": 9, "reason": "cao trào giả lập"}]
    else:
        seg = []   # đoạn tẻ nhạt → Gemini trả mảng rỗng (đúng như hướng dẫn trong prompt)
    return "```json\n" + json.dumps(seg, ensure_ascii=False) + "\n```"


srv = FakeGeminiServer()
srv.reply_fn = fake_reply

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    video = tmp / "demo.mp4"
    make_video(video, 30.0, with_audio=False)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")
    media = probe_media(video)

    cfg = Config(video_path=video, output_dir=tmp / "out", engine="web", user_data_dir=tmp / "profile",
                browser_channel="chromium", headless=True, gemini_url=srv.url(), web_stable_sec=0.5,
                web_response_timeout_sec=25, web_upload_timeout_sec=15, web_delay_between_prompts_sec=0,
                web_retries=2, frames_per_prompt=4, highlight_frame_interval_sec=2.0, highlight_target_ratio=0.5)
    cfg.ensure_dirs()
    setup_logging(cfg.log_path)

    drv = gwd.GeminiWebDriver(cfg)
    drv.start()
    try:
        vision = GeminiWebVisionEngine(drv)
        windows = plan_highlight_windows(vision, cfg, media)
    finally:
        drv.close()

    check(f"tìm được ít nhất 1 đoạn highlight: {windows}", len(windows) >= 1)
    s, e = windows[0]
    check(f"đoạn tìm được nằm ĐÚNG trong vùng 'hay' giả lập [18s,24s]: [{s:.1f}s, {e:.1f}s]",
          17.0 <= s <= 19.0 and 23.0 <= e <= 25.0)
    total = sum(w[1] - w[0] for w in windows)
    check(f"tổng thời lượng chọn được không vượt quá mục tiêu (50% × 30s = 15s): {total:.1f}s ≤ 15s", total <= 15.0 + 0.5)

srv.close()
print("TẤT CẢ PASS ✔")
