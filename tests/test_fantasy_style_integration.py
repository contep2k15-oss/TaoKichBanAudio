"""python tests/test_fantasy_style_integration.py — xác nhận story_hook mang xuyên suốt nhiều chunk trong
pipeline THẬT (Playwright + video giả lập nhiều chunk), không chỉ test hàm build_batch_prompt đơn lẻ."""
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
from utils import format_timestamp, parse_timestamp, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


HOOK_MARKER = "CÂU CHUYỆN GIẢ TƯỞNG"
CALLBACK_MARKER = "THỈNH THOẢNG"
seen_prompts: list[tuple[str, str]] = []   # (label, prompt) cho MỌI lần Gemini nhận prompt


def fake_reply(req: dict) -> str:
    import json
    import re
    prompt = req["prompt"]
    seen_prompts.append(prompt)
    stamps = [parse_timestamp(m) for m in re.findall(r"Ảnh \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    segs = []
    for k, t in enumerate(stamps, start=1):
        # đoạn đầu tiên của TOÀN VIDEO (chunk 0, lô 1): trả về text CỐ ĐỊNH để test nhận diện lại đúng câu hook
        if HOOK_MARKER in prompt and k == 1:
            text = "Ngày xửa ngày xưa có một chú robot nhỏ lạc giữa khu rừng dữ liệu."
        else:
            text = "một hai ba bốn năm"
        segs.append({"id": k, "start_time": format_timestamp(t - 0.4), "end_time": format_timestamp(t + 0.4),
                    "text": text, "tone": "vui ve"})
    return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"


srv = FakeGeminiServer()
srv.reply_fn = fake_reply
edge_tts.Communicate = FakeCommunicate

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    video = tmp / "story.mp4"
    make_video(video, 24.0, with_audio=False)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")
    cfg = web_cfg(tmp, srv, video, "out_story", chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0,
                 narrative_style="fantasy_inspiring", narrator_pov="Người kể chuyện bí ẩn")
    setup_logging(cfg.log_path)

    out = app.run_pipeline(cfg)
    check("pipeline chạy xong, có video", out["video"] is not None and Path(out["video"]).is_file())

    opening_prompts = [p for p in seen_prompts if HOOK_MARKER in p]
    callback_prompts = [p for p in seen_prompts if CALLBACK_MARKER in p]
    check(f"CHỈ ĐÚNG 1 lần yêu cầu mở đầu bằng câu chuyện giả tưởng (thực tế: {len(opening_prompts)} lần)",
          len(opening_prompts) == 1)
    check(f"CÓ ít nhất 1 lần yêu cầu lồng ghép lại câu chuyện ở chunk sau (thực tế: {len(callback_prompts)} lần)",
          len(callback_prompts) >= 1)
    check("mọi prompt callback đều chứa ĐÚNG nội dung câu chuyện mở đầu đã sinh ra (chú robot lạc giữa rừng dữ liệu)",
          all("chú robot nhỏ lạc giữa khu rừng dữ liệu" in p for p in callback_prompts))
    batch_prompts = [p for p in seen_prompts if "Ảnh 1:" in p]   # chỉ prompt VIẾT KỊCH BẢN (loại trừ prompt "rút gọn câu")
    check(f"mọi prompt viết kịch bản (mọi chunk, {len(batch_prompts)} lô) đều có chỉ dẫn ngôi kể 'Người kể chuyện bí ẩn'",
          batch_prompts and all("Người kể chuyện bí ẩn" in p for p in batch_prompts))

    log_text = cfg.log_path.read_text(encoding="utf-8")
    check("log xác nhận đã trích đúng câu chuyện mở đầu MỘT LẦN DUY NHẤT",
          log_text.count("Đã trích câu chuyện mở đầu") == 1 and "chú robot nhỏ lạc giữa khu rừng dữ liệu" in log_text)

srv.close()
print("TẤT CẢ PASS ✔")
