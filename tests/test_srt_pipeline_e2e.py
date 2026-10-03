"""python tests/test_srt_pipeline_e2e.py — export_srt=True chạy TRỌN qua long_video_pipeline.run() thật,
ra đúng file .srt khớp với script.json; export_srt=False (mặc định) KHÔNG tạo gì thêm, không đổi hành vi cũ."""
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


def fake_reply(req: dict) -> str:
    prompt = req["prompt"]
    stamps = [parse_timestamp(m) for m in re.findall(r"Image \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    segs = [{"id": k, "start_time": format_timestamp(t - 0.4), "end_time": format_timestamp(t + 0.4),
            "text": "Một câu thuyết minh ngắn.", "tone": "vui ve"} for k, t in enumerate(stamps, start=1)]
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

    # ── 1) export_srt=True ──
    cfg1 = web_cfg(tmp, srv, video, "out_srt", export_srt=True)
    setup_logging(cfg1.log_path)
    out1 = run(cfg1, interactive=False)
    check("pipeline chạy xong, có video", out1["video"] is not None and Path(out1["video"]).is_file())
    check("CÓ file .srt trong kết quả trả về", out1["srt"] is not None and Path(out1["srt"]).is_file())
    check("file .srt nằm ĐÚNG cạnh video/script (output_dir)", out1["srt"].parent == cfg1.output_dir)

    srt_text = out1["srt"].read_text(encoding="utf-8")
    script_data = json.loads(out1["script"].read_text(encoding="utf-8"))
    check(f"số mục phụ đề KHỚP số đoạn kịch bản (không chia thêm, vì mỗi câu đã ngắn): {srt_text.count('-->')} == {len(script_data)}",
          srt_text.count("-->") == len(script_data))
    check("nội dung câu đầu tiên trong .srt khớp đúng script.json", script_data[0]["text"] in srt_text)
    check("định dạng mốc thời gian ĐÚNG chuẩn SRT (dấu phẩy mili-giây)", re.search(r"\d\d:\d\d:\d\d,\d\d\d -->", srt_text))

    # ── 2) export_srt=False (mặc định) — KHÔNG tạo file .srt, không đổi hành vi cũ ──
    cfg2 = web_cfg(tmp, srv, video, "out_nosrt")   # không truyền export_srt → dùng mặc định False
    setup_logging(cfg2.log_path)
    out2 = run(cfg2, interactive=False)
    check("mặc định (export_srt=False): outputs['srt'] = None", out2["srt"] is None)
    check("mặc định: KHÔNG tạo file output.srt nào trên đĩa", not cfg2.srt_path.exists())
    check("mặc định: video/script vẫn ra bình thường như trước đây", out2["video"] is not None and out2["script"] is not None)

srv.close()
print("TẤT CẢ PASS ✔")
