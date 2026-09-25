"""python tests/test_youtube_pipeline_e2e.py — luồng YouTube-link chạy TRỌN qua long_video_pipeline.run().
Giả lập download_youtube_video() (copy file cục bộ) vì sandbox không có mạng ra youtube.com — phần ĐỊNH
DANH đường dẫn/URL, song song hoá, phân phối script vào chunk, và RESUME (không gọi lại) đều là mã thật."""
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
import main as app  # noqa: E402
import youtube_source  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from smoke_test import FakeCommunicate, make_video  # noqa: E402
from utils import format_timestamp, probe_media, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


DURATION = 20.0
call_count = {"n": 0}


def fake_reply(req: dict) -> str:
    call_count["n"] += 1
    prompt = req["prompt"]
    assert "youtube.com/watch" in prompt, "prompt phải chứa nguyên văn link YouTube"
    assert "XEM TRỰC TIẾP" in prompt, "prompt phải yêu cầu Gemini xem trực tiếp video"
    # trả về kịch bản PHỦ TOÀN BỘ video 20s, không cần frame nào đính kèm (đúng thiết kế: images=())
    segs = []
    t = 0.5
    i = 1
    while t < DURATION - 1.0:
        segs.append({"id": i, "start_time": format_timestamp(t), "end_time": format_timestamp(t + 1.2),
                    "text": "một hai ba bốn", "tone": "vui ve"})
        t += 2.5
        i += 1
    return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"


def fake_download(url: str, out_path: Path, **kw) -> Path:
    """Thay vì gọi yt-dlp thật (sandbox không có mạng), copy 1 video giả lập đã tạo sẵn — vẫn giữ đúng
    HÀNH VI: mất một chút thời gian (mô phỏng tải), rồi tạo ra file tại đúng out_path."""
    time.sleep(1.0)
    shutil.copy(SRC_VIDEO, out_path)
    return out_path


srv = FakeGeminiServer()
srv.reply_fn = fake_reply
edge_tts.Communicate = FakeCommunicate
youtube_source.download_youtube_video = fake_download

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    SRC_VIDEO = tmp / "src_for_fake_download.mp4"
    make_video(SRC_VIDEO, DURATION, with_audio=False)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")

    from config import Config
    import config as config_mod
    config_mod.DEFAULT_PROFILE_DIR = tmp / "profile"
    orig_post = Config.__post_init__
    def patched_post(self):
        orig_post(self)
        self.gemini_url = srv.url()
    Config.__post_init__ = patched_post

    cfg = Config(video_path=None, youtube_url="https://www.youtube.com/watch?v=fakeID12345",
                output_dir=tmp / "out_yt", engine="web", browser_channel="chromium", headless=True,
                web_stable_sec=0.5, web_response_timeout_sec=25, web_upload_timeout_sec=15,
                web_delay_between_prompts_sec=0, web_retries=2, chunk_target_sec=8.0, chunk_tolerance_sec=3.0,
                min_chunk_sec=4.0)
    setup_logging(cfg.log_path)

    t0 = time.perf_counter()
    out = app.run_pipeline(cfg)
    elapsed = time.perf_counter() - t0
    check("pipeline chạy xong, có video", out["video"] is not None and Path(out["video"]).is_file())
    check("chỉ gọi Gemini ĐÚNG 1 LẦN cho toàn bộ video (không chia lô theo ảnh)", call_count["n"] == 1)

    info = probe_media(out["video"])
    check(f"video cuối đúng độ dài gốc (đã tải về): {info.duration_sec:.1f}s ≈ {DURATION}s",
          abs(info.duration_sec - DURATION) < 0.5)

    log_text = cfg.log_path.read_text(encoding="utf-8")
    check("log xác nhận đã chạy song song tải + hỏi Gemini", "chạy nền) VÀ gửi link" in log_text)
    check("log xác nhận nhận được nhiều đoạn từ 1 lần gọi", re.search(r"Đã nhận \d+ đoạn thuyết minh", log_text))

    # ── Chạy lại lần 2 (mô phỏng resume): KHÔNG được tải lại / gọi Gemini lại ──
    call_count["n"] = 0
    downloaded_before = (cfg.workdir / "youtube_source.mp4").stat().st_mtime
    out2 = app.run_pipeline(cfg)
    check("resume: KHÔNG gọi lại Gemini lần thứ 2", call_count["n"] == 0)
    check("resume: KHÔNG tải lại video (mtime không đổi)",
          (cfg.workdir / "youtube_source.mp4").stat().st_mtime == downloaded_before)
    check("resume: vẫn ra đúng video kết quả", out2["video"] is not None and Path(out2["video"]).is_file())

srv.close()
print("TẤT CẢ PASS ✔")
