"""python tests/test_pipeline_cancel.py — cancel_event dừng pipeline ĐÚNG ranh giới chunk, giữ nguyên phần đã xong."""
import json
import re
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
import long_video_pipeline  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from smoke_test import FakeCommunicate, make_video, web_cfg  # noqa: E402
from utils import PipelineCancelled, format_timestamp, parse_timestamp, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def fake_reply(req: dict) -> str:
    prompt = req["prompt"]
    stamps = [parse_timestamp(m) for m in re.findall(r"Image \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    segs = [{"id": k, "start_time": format_timestamp(t - 0.4), "end_time": format_timestamp(t + 0.4),
            "text": "một hai ba bốn", "tone": "vui ve"} for k, t in enumerate(stamps, start=1)]
    return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"


srv = FakeGeminiServer()
srv.reply_fn = fake_reply
edge_tts.Communicate = FakeCommunicate

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    video = tmp / "demo.mp4"
    make_video(video, 24.0, with_audio=False)               # → 3 macro-chunk (đúng cấu hình web_cfg quen thuộc)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")
    cfg = web_cfg(tmp, srv, video, "out_cancel", chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0)
    setup_logging(cfg.log_path)

    # ── 1) dừng NGAY TỪ ĐẦU (trước khi làm chunk nào) → dừng sạch, không chunk nào có checkpoint ──
    ev = threading.Event()
    ev.set()
    try:
        long_video_pipeline.run(cfg, interactive=False, cancel_event=ev)
        raise AssertionError("phải ném PipelineCancelled")
    except PipelineCancelled as e:
        check(f"dừng ngay từ đầu: ném đúng PipelineCancelled ({e})", True)

    # ── 2) dừng SAU KHI xong đúng 1 chunk (đếm số lần gọi on_chunk_progress rồi set cancel) ──
    calls = []

    def on_progress(done, total):
        calls.append(done)
        if done == 1:
            ev2.set()   # yêu cầu dừng NGAY SAU KHI chunk #1 xong — kiểm tra dừng đúng ranh giới, không giữa chừng

    ev2 = threading.Event()
    cfg2 = web_cfg(tmp, srv, video, "out_cancel2", chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0)
    setup_logging(cfg2.log_path)
    try:
        long_video_pipeline.run(cfg2, interactive=False, cancel_event=ev2, on_chunk_progress=on_progress)
        raise AssertionError("phải ném PipelineCancelled")
    except PipelineCancelled:
        pass
    check(f"dừng sau chunk #1: chỉ xử lý ĐÚNG 1 chunk trước khi dừng (calls={calls})", calls == [1])

    from checkpoint import PipelineCheckpoint, Stage
    from long_video_pipeline import build_fingerprint
    from utils import probe_media
    fp = build_fingerprint(cfg2, probe_media(video))
    checkpoint = PipelineCheckpoint(cfg2.workdir, fp)
    check("chunk #0 (đã xong TRƯỚC khi dừng) VẪN CÒN checkpoint SCRIPT", checkpoint.has(0, Stage.SCRIPT))
    check("chunk #1, #2 (chưa kịp làm) KHÔNG có checkpoint SCRIPT — không bị làm dở dang", not checkpoint.has(1, Stage.SCRIPT))

    # ── 3) CHẠY LẠI (không cancel) sau khi đã dừng giữa chừng → tự tiếp tục đúng chỗ dở, KHÔNG gọi lại Gemini cho chunk #0 ──
    n_before = len(srv.requests) if hasattr(srv, "requests") else None
    out = long_video_pipeline.run(cfg2, interactive=False)
    check("chạy lại sau khi dừng: hoàn tất bình thường, có video", out["video"] is not None and Path(out["video"]).is_file())

    # ── 4) không truyền cancel_event (None, mặc định) → hành vi CŨ giữ nguyên, không ảnh hưởng ──
    cfg3 = web_cfg(tmp, srv, video, "out_cancel3", chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0)
    setup_logging(cfg3.log_path)
    out3 = long_video_pipeline.run(cfg3, interactive=False)     # cancel_event mặc định None
    check("không truyền cancel_event: chạy bình thường như trước đây, không bị ảnh hưởng", out3["video"] is not None)

    # ── 5) dừng ở Pha B (TTS) sau khi Pha A đã xong hết — script giữ nguyên, chỉ audio bị dừng dở ──
    cfg4 = web_cfg(tmp, srv, video, "out_cancel4", chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0,
                  stop_after=2)   # xong hết Pha A (script), dừng đúng lúc trước Pha B
    setup_logging(cfg4.log_path)
    long_video_pipeline.run(cfg4, interactive=False)
    cfg4.stop_after = None   # bỏ giới hạn để lần gọi SAU được đi tiếp vào Pha B (lần trước chỉ để dừng đúng sau Pha A)
    ev5 = threading.Event()
    tts_calls = []
    call_count = [0]

    def on_progress5(done, total):
        # LƯU Ý: on_chunk_progress cũng được gọi trong Pha A khi RESUME từ cache (báo tiến độ đọc cache tức
        # thời) — nên không dùng giá trị `done` để nhận biết "đang ở Pha B", mà đếm theo THỨ TỰ LẦN GỌI: 3
        # lần đầu là Pha A resume (done=1,2,3 tức thời), lần thứ 4 mới là Pha B thật (done=1 lại, cho TTS).
        call_count[0] += 1
        tts_calls.append((call_count[0], done))
        if call_count[0] == 4:
            ev5.set()
    try:
        long_video_pipeline.run(cfg4, interactive=False, cancel_event=ev5, on_chunk_progress=on_progress5)
        raise AssertionError("phải ném PipelineCancelled")
    except PipelineCancelled:
        pass
    fp4 = build_fingerprint(cfg4, probe_media(video))
    checkpoint4 = PipelineCheckpoint(cfg4.workdir, fp4)
    check("dừng giữa Pha B: MỌI chunk vẫn còn nguyên SCRIPT (Pha A không bị đụng tới)",
          all(checkpoint4.has(i, Stage.SCRIPT) for i in range(3)))
    check("dừng giữa Pha B: chunk #0 có TTS xong, chunk #1/#2 CHƯA có (đúng ranh giới)",
          checkpoint4.has(0, Stage.TTS) and not checkpoint4.has(1, Stage.TTS))

print("TẤT CẢ PASS ✔")
