"""python tests/test_preview.py — build_preview_source() cắt đúng số giây; run_preview() chạy TRỌN VẸN
pipeline thật trên đoạn ngắn, giữ nguyên mọi lựa chọn nội dung/giọng đọc của cfg gốc, không đụng output/
workdir của lần chạy chính."""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
from config import Config  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from preview import MAX_PREVIEW_SEC, MIN_PREVIEW_SEC, build_preview_config, build_preview_source, run_preview  # noqa: E402
from smoke_test import FakeCommunicate, make_video, web_cfg  # noqa: E402
from utils import format_timestamp, parse_timestamp, probe_media  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def fake_reply(req: dict) -> str:
    """LƯU Ý: server giả lập cần nhận lại một CHUỖI (giống văn bản Gemini thật trả về, có khối ```json),
    KHÔNG PHẢI list Python thô — trả nhầm list khiến phía automation không đọc được, treo tới hết timeout
    (lỗi thật từng gặp khi viết test này — không phải lỗi trong preview.py)."""
    import json
    prompt = req["prompt"]
    stamps = [parse_timestamp(m) for m in re.findall(r"Image \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    segs = [{"id": k, "start_time": format_timestamp(t - 0.3), "end_time": format_timestamp(t + 0.3),
            "text": "Một câu ví dụ.", "tone": "vui ve"} for k, t in enumerate(stamps, start=1)]
    return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    long_video = tmp / "long.mp4"
    make_video(long_video, 60.0, with_audio=False)

    # ── 1) build_preview_source: cắt ĐÚNG số giây yêu cầu ──
    out1 = tmp / "cut25.mp4"
    media = probe_media(long_video)
    result = build_preview_source(long_video, 25.0, out1, media)
    info = probe_media(result)
    check(f"cắt đúng ~25s (thực tế {info.duration_sec:.1f}s)", abs(info.duration_sec - 25.0) < 1.0)
    check("file cắt KHÁC file gốc (không trả nhầm video dài)", result != long_video)

    # ── 2) video gốc NGẮN HƠN số giây yêu cầu → trả về NGUYÊN video gốc, không cắt gì ──
    short_video = tmp / "short.mp4"
    make_video(short_video, 8.0, with_audio=False)
    out2 = tmp / "cut_short.mp4"
    result2 = build_preview_source(short_video, 25.0, out2, probe_media(short_video))
    check("video ngắn hơn yêu cầu → trả về ĐÚNG video gốc (không tạo file cắt thừa)", result2 == short_video)
    check("KHÔNG tạo ra file cắt khi không cần", not out2.is_file())

    # ── 3) giới hạn MIN/MAX — số giây quá nhỏ/quá lớn phải tự kẹp lại ──
    out3 = tmp / "cut_clamped.mp4"
    build_preview_source(long_video, 0.5, out3, media)              # yêu cầu 0.5s → phải kẹp lên MIN_PREVIEW_SEC
    info3 = probe_media(out3)
    check(f"yêu cầu 0.5s (< tối thiểu) → tự kẹp lên {MIN_PREVIEW_SEC}s (thực tế {info3.duration_sec:.1f}s)",
          abs(info3.duration_sec - MIN_PREVIEW_SEC) < 1.0)
    very_long_video = tmp / "very_long.mp4"
    make_video(very_long_video, 90.0, with_audio=False)              # dài hơn hẳn MAX_PREVIEW_SEC để kiểm đúng ngưỡng trần
    out4 = tmp / "cut_max.mp4"
    build_preview_source(very_long_video, 999.0, out4, probe_media(very_long_video))   # yêu cầu 999s → phải kẹp còn MAX_PREVIEW_SEC
    info4 = probe_media(out4)
    check(f"yêu cầu 999s (> tối đa) → tự kẹp còn {MAX_PREVIEW_SEC}s (thực tế {info4.duration_sec:.1f}s)",
          abs(info4.duration_sec - MAX_PREVIEW_SEC) < 1.0)

    # ── 4) build_preview_config: giữ NGUYÊN lựa chọn nội dung, tách RIÊNG thư mục, luôn force ──
    cfg = Config(video_path=long_video, output_dir=tmp / "out_main", narrative_style="humorous",
                narrator_pov="Tôi", creativity_level=0.7, narration_purpose="review", force=False)
    pcfg = build_preview_config(cfg, out1)
    check("preview_config: GIỮ NGUYÊN narrative_style/narrator_pov/creativity_level/narration_purpose",
          pcfg.narrative_style == "humorous" and pcfg.narrator_pov == "Tôi" and
          pcfg.creativity_level == 0.7 and pcfg.narration_purpose == "review")
    check("preview_config: video_path đổi sang file đã cắt", pcfg.video_path == out1)
    check("preview_config: output_dir TÁCH RIÊNG (thư mục con 'preview'), không đụng output chính",
          pcfg.output_dir == cfg.output_dir / "preview" and pcfg.output_dir != cfg.output_dir)
    check("preview_config: luôn force=True (không phụ thuộc cfg gốc)", pcfg.force is True)
    check("preview_config: stop_after=None (chạy ĐẦY ĐỦ, không dừng giữa chừng)", pcfg.stop_after is None)
    check("cfg GỐC không bị thay đổi gì (dataclasses.replace tạo bản SAO)", cfg.output_dir == tmp / "out_main" and cfg.force is False)

    # ── 5) run_preview(): chạy TRỌN VẸN qua pipeline thật, dùng Gemini/TTS giả lập ──
    srv = FakeGeminiServer()
    srv.reply_fn = fake_reply
    edge_tts.Communicate = FakeCommunicate
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")

    cfg5 = web_cfg(tmp, srv, long_video, "out_real", narrative_style="formal")
    out = run_preview(cfg5, seconds=20.0)
    check("run_preview(): pipeline chạy xong, có video kết quả", out["video"] is not None and Path(out["video"]).is_file())
    preview_info = probe_media(out["video"])
    check(f"video xem trước đúng ~20s (thực tế {preview_info.duration_sec:.1f}s), KHÔNG phải cả video 60s gốc",
          15.0 <= preview_info.duration_sec <= 23.0)
    check("output nằm trong thư mục con 'preview', TÁCH RIÊNG khỏi output chính",
          "preview" in str(out["video"]) and not (tmp / "out_real" / "output_dubbed_video.mp4").is_file())
    srv.close()

    # ── 6) chưa chọn video/link nào → báo lỗi rõ ràng, không crash mơ hồ ──
    cfg6 = Config(video_path=None, output_dir=tmp / "out_none")
    try:
        run_preview(cfg6, seconds=20.0)
        raise AssertionError("phải báo lỗi")
    except Exception as e:
        check(f"chưa chọn video: báo lỗi rõ ràng, dễ hiểu: {e}", "chọn video" in str(e) or "youtube" in str(e).lower())

print("TẤT CẢ PASS ✔")
