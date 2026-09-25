"""python tests/test_highlight_pipeline_e2e.py — highlight_mode chạy TRỌN qua long_video_pipeline.run()."""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
import main as app  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from smoke_test import FakeCommunicate, make_video, web_cfg  # noqa: E402
from utils import format_timestamp, parse_timestamp, probe_media, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


HOT_ZONE = (10.0, 20.0)   # "đoạn hay" giả lập trong video 30s


def fake_reply(req: dict) -> str:
    prompt = req["prompt"]
    if "đắt giá" in prompt or "highlight" in prompt.lower() or "score" in prompt:
        m = re.search(r"trong khoảng \[(\d\d:\d\d:\d\d\.\d+), (\d\d:\d\d:\d\d\.\d+)\]", prompt)
        t0, t1 = parse_timestamp(m.group(1)), parse_timestamp(m.group(2))
        overlap = max(0.0, min(t1, HOT_ZONE[1]) - max(t0, HOT_ZONE[0]))
        seg = ([{"start_time": format_timestamp(max(t0, HOT_ZONE[0])), "end_time": format_timestamp(min(t1, HOT_ZONE[1])),
                "score": 9, "reason": "hay"}] if overlap > 1.0 else [])
        return "```json\n" + json.dumps(seg, ensure_ascii=False) + "\n```"
    # prompt viết KỊCH BẢN bình thường (áp cho video ĐàCẮT)
    stamps = [parse_timestamp(m) for m in re.findall(r"Ảnh \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    segs = [{"id": k, "start_time": format_timestamp(t - 0.4), "end_time": format_timestamp(t + 0.4),
            "text": "một hai ba bốn", "tone": "vui ve"} for k, t in enumerate(stamps, start=1)]
    return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"


srv = FakeGeminiServer()
srv.reply_fn, srv.requests = fake_reply, []
edge_tts.Communicate = FakeCommunicate

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    video = tmp / "long.mp4"
    make_video(video, 30.0, with_audio=False)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")
    info_before = probe_media(video)

    cfg = web_cfg(tmp, srv, video, "out_hl", chunk_target_sec=6.0, chunk_tolerance_sec=2.0, min_chunk_sec=3.0,
                 highlight_mode=True, highlight_target_ratio=0.4, highlight_frame_interval_sec=2.0)
    setup_logging(cfg.log_path)

    out = app.run_pipeline(cfg)
    check("pipeline chạy xong, có video", out["video"] is not None and Path(out["video"]).is_file())

    info_after = probe_media(out["video"])
    check(f"video CUỐI ngắn hơn hẳn video gốc (30s → {info_after.duration_sec:.1f}s, ~40% mục tiêu)",
          info_after.duration_sec < info_before.duration_sec * 0.6)
    check(f"video cuối bao trùm đúng vùng 'hay' giả lập (10-20s), độ dài hợp lý (~{info_after.duration_sec:.1f}s ≈ 10-13s)",
          8.0 <= info_after.duration_sec <= 14.0)

    log_text = cfg.log_path.read_text(encoding="utf-8")
    check("log xác nhận đã chuyển sang xử lý video ĐÃ CẮT cho phần còn lại của pipeline",
          "toàn bộ pipeline xử lý trên video ĐÃ CẮT" in log_text)

    # chạy lại LẦN 2 với ĐÚNG cấu hình: phải dùng lại video highlight đã cache, KHÔNG quét Gemini highlight lại
    n_before = len([r for r in srv.requests if "score" in r.get("prompt", "")])
    cfg2 = web_cfg(tmp, srv, video, "out_hl", chunk_target_sec=6.0, chunk_tolerance_sec=2.0, min_chunk_sec=3.0,
                   highlight_mode=True, highlight_target_ratio=0.4, highlight_frame_interval_sec=2.0)
    app.run_pipeline(cfg2)
    n_after = len([r for r in srv.requests if "score" in r.get("prompt", "")])
    check("chạy lại (cùng cấu hình): KHÔNG quét lại highlight (dùng cache)", n_after == n_before)

print("TẤT CẢ PASS ✔")
