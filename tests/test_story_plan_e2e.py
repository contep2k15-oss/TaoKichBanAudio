"""python tests/test_story_plan_e2e.py — enable_story_plan=True chạy TRỌN qua long_video_pipeline.run() thật."""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
from long_video_pipeline import run  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from smoke_test import FakeCommunicate, make_video, web_cfg  # noqa: E402
from utils import format_timestamp, parse_timestamp, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


n_outline_calls = [0]


def fake_reply(req: dict) -> str:
    prompt = req["prompt"]
    if '"outline"' in prompt:
        n_outline_calls[0] += 1
        return '```json\n[{"outline": "Mở đầu nhẹ nhàng, phát triển dần, kết thúc ấm áp."}]\n```'
    stamps = [parse_timestamp(m) for m in re.findall(r"Image \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    segs = [{"id": k, "start_time": format_timestamp(t - 0.3), "end_time": format_timestamp(t + 0.3),
            "text": "Một câu ví dụ.", "tone": "vui ve"} for k, t in enumerate(stamps, start=1)]
    return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"


srv = FakeGeminiServer()
srv.reply_fn = fake_reply
edge_tts.Communicate = FakeCommunicate

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    video = tmp / "demo.mp4"
    make_video(video, 10.0, with_audio=False)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")

    cfg = web_cfg(tmp, srv, video, "out_plan", enable_story_plan=True)
    setup_logging(cfg.log_path)
    out = run(cfg, interactive=False)
    check("pipeline chạy xong, có video", out["video"] is not None and Path(out["video"]).is_file())
    check(f"có ĐÚNG 1 lượt gọi dàn ý (video ngắn = 1 chunk): {n_outline_calls[0]}", n_outline_calls[0] == 1)

srv.close()
print("TẤT CẢ PASS ✔")
