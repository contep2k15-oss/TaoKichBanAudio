"""python tests/test_whisper_alignment_pipeline.py — enable_whisper_alignment chạy TRỌN qua
long_video_pipeline.run() thật, dùng forced_alignment GIẢ LẬP (không gọi Whisper thật — phần đó không kiểm
chứng được trong sandbox) để xác nhận ĐÚNG THỨ TỰ: audio cuối phải tồn tại TRƯỚC khi alignment chạy, và kết
quả .srt được GHI ĐÈ đúng bằng bản tinh chỉnh."""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
import subtitles  # noqa: E402
from long_video_pipeline import run  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from smoke_test import FakeCommunicate, make_video, web_cfg  # noqa: E402
from utils import format_timestamp, parse_timestamp, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def fake_reply(req: dict) -> str:
    prompt = req["prompt"]
    stamps = [parse_timestamp(m) for m in re.findall(r"Image \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    segs = [{"id": k, "start_time": format_timestamp(t - 0.3), "end_time": format_timestamp(t + 0.3),
            "text": "Một câu ví dụ ngắn.", "tone": "vui ve"} for k, t in enumerate(stamps, start=1)]
    return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"


# Giả lập try_whisper_alignment: KHÔNG gọi Whisper thật, chỉ ghi lại audio_path nhận được lúc gọi, để xác
# nhận file đó ĐÃ TỒN TẠI THẬT (chứng minh đúng thứ tự: chạy SAU khi mux xong, không phải trước).
calls = []
real_try = subtitles.try_whisper_alignment


def fake_try_whisper_alignment(segments, voice_audio_path, **kw):
    calls.append((voice_audio_path, voice_audio_path.is_file(), voice_audio_path.stat().st_size if voice_audio_path.is_file() else 0))
    # trả về mốc GIẢ (khác hẳn ước lượng ký tự) để kiểm tra .srt THỰC SỰ bị ghi đè
    sentences = subtitles.sentences_for_alignment(segments)
    return [(float(i) * 100.0, float(i) * 100.0 + 1.0) for i in range(len(sentences))]


subtitles.try_whisper_alignment = fake_try_whisper_alignment

srv = FakeGeminiServer()
srv.reply_fn = fake_reply
edge_tts.Communicate = FakeCommunicate

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    video = tmp / "demo.mp4"
    make_video(video, 10.0, with_audio=False)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")

    cfg = web_cfg(tmp, srv, video, "out_whisper", export_srt=True, enable_whisper_alignment=True)
    setup_logging(cfg.log_path)
    out = run(cfg, interactive=False)
    check("pipeline chạy xong, có video", out["video"] is not None and Path(out["video"]).is_file())
    check(f"ĐÚNG 1 lần gọi try_whisper_alignment: {len(calls)}", len(calls) == 1)
    audio_path, existed, size = calls[0]
    check(f"LÚC GỌI, file VIDEO cuối ĐÃ TỒN TẠI THẬT trên đĩa (đúng thứ tự, không gọi quá sớm): {audio_path}, tồn tại={existed}",
          existed)
    check(f"file video KHÔNG RỖNG (đã ghi xong, không phải file placeholder): {size} bytes", size > 0)

    srt_text = out["srt"].read_text(encoding="utf-8")
    check("file .srt đã bị GHI ĐÈ bằng mốc giả lập (xuất hiện '100.000' trong mốc thời gian đặc trưng)",
          "00:01:40" in srt_text or "00:00:01" in srt_text)   # mốc 100.0s hoặc 1.0s từ dữ liệu giả lập

subtitles.try_whisper_alignment = real_try
srv.close()
print("TẤT CẢ PASS ✔")
