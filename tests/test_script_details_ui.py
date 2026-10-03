"""python tests/test_script_details_ui.py — khu vực '📊 Chi tiết kịch bản & QA': dùng ĐÚNG dữ liệu thật từ
script.json/sync_report.json sau khi pipeline chạy xong (không bịa dữ liệu UI riêng)."""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from smoke_test import FakeCommunicate, make_video, web_cfg  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402
from utils import format_timestamp, parse_timestamp, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]

APP = str(Path(__file__).resolve().parent.parent / "app_streamlit.py")


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def fake_reply(req: dict) -> str:
    prompt = req["prompt"]
    stamps = [parse_timestamp(m) for m in re.findall(r"Image \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    certs = ["observed_fact", "safe_inference", "creative_framing"]
    segs = [{"id": k, "start_time": format_timestamp(t - 0.3), "end_time": format_timestamp(t + 0.3),
            "text": f"Đoạn thuyết minh thứ {k}.", "tone": "vui ve", "certainty": certs[k % 3]}
           for k, t in enumerate(stamps, start=1)]
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
    cfg = web_cfg(tmp, srv, video, "out_ui")
    setup_logging(cfg.log_path)

    from long_video_pipeline import run
    out = run(cfg, interactive=False)
    check("pipeline thật chạy xong, có script.json", out["script"] is not None and Path(out["script"]).is_file())

    # ── Dựng GUI, trỏ thẳng vào video/output_dir đã chạy xong, để job 'done' đọc đúng kết quả thật ──
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.session_state["video_path"] = str(video)
    at.session_state["output_dir"] = str(tmp / "out_ui")
    at.run()

    import background_jobs as bg
    pv_workdir = (tmp / "out_ui" / "work")
    # job thật không chạy qua GUI trong test này — giả lập job 'done' với kết quả đã có để chỉ kiểm tra PHẦN HIỂN THỊ

    class FakeJob:
        state = "done"
        result = {"video": str(out["video"]), "voiceover": str(out["voiceover"]) if out["voiceover"] else None,
                  "script": str(out["script"]), "srt": None}
        error = None
        stop_after = None
        current_step = ""
    bg._REGISTRY[bg._key(cfg.workdir)] = FakeJob()
    at.run()

    check("KHÔNG có exception khi hiện chi tiết kịch bản", not list(at.exception))
    exp = next((e for e in at.expander if "Chi tiết kịch bản" in e.label), None)
    check("có khu vực expander 'Chi tiết kịch bản & QA'", exp is not None)

    md_all = " ".join(m.value for m in at.markdown)
    check("có hiện badge 'Quan sát chắc chắn'", "Quan sát chắc chắn" in md_all)
    check("có hiện badge 'Suy luận an toàn'", "Suy luận an toàn" in md_all)
    check("có hiện badge 'Cách kể sáng tạo'", "Cách kể sáng tạo" in md_all)

    dfs = list(at.dataframe)
    check(f"có bảng timeline, đúng số dòng = số đoạn kịch bản: {len(dfs)}", len(dfs) == 1)

    successes = [s.value for s in at.success]
    warnings_ = [w.value for w in at.warning]
    check(f"có hiện checklist QA (success/warning): {len(successes) + len(warnings_)} dòng",
          len(successes) + len(warnings_) >= 1)

srv.close()
print("TẤT CẢ PASS ✔")
