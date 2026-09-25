"""python tests/test_pipeline_status.py — dùng lại hạ tầng giả lập Gemini/Edge-TTS của smoke_test.py."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
import main as app  # noqa: E402
import pipeline_status  # noqa: E402
from checkpoint import PipelineCheckpoint, Stage  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from smoke_test import FakeCommunicate, fake_reply, make_video, web_cfg  # noqa: E402
from utils import setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


edge_tts.Communicate = FakeCommunicate
srv = FakeGeminiServer()
srv.reply_fn, srv.requests = fake_reply, []

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    video = tmp / "status_demo.mp4"
    make_video(video, 24.0, with_audio=False)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")
    cfg = web_cfg(tmp, srv, video, "out_status", chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0)
    setup_logging(cfg.log_path)

    # 1) chưa chạy gì cả → read_status() = None
    st = pipeline_status.read_status(cfg)
    check("video chưa từng chạy → read_status() = None", st is None)

    # 2) chạy full → mọi chunk phải "done"
    app.run_pipeline(cfg)
    st = pipeline_status.read_status(cfg)
    check("sau khi chạy xong: đọc được trạng thái, không None", st is not None)
    check(f"tất cả {st.n_total} chunk đều done", st.n_done == st.n_total and st.n_total > 1)
    check("all_tts_ready = True khi mọi chunk đã xong", st.all_tts_ready)
    check("summary_line báo đúng 'xử lý xong hoàn toàn'", "xong hoàn toàn" in st.summary_line())

    # 3) MÔ PHỎNG đúng tình huống người dùng gặp: xoá dở TTS+ASSEMBLED của 1 chunk giữa chừng
    fp = pipeline_status.build_fingerprint(cfg, __import__("utils").probe_media(cfg.video_path))
    checkpoint = PipelineCheckpoint(cfg.workdir, fp)
    mid = st.n_total // 2
    checkpoint.invalidate_from(mid, Stage.TTS)   # chunk giữa: còn EXTRACTED+SCRIPT, mất TTS+ASSEMBLED

    st2 = pipeline_status.read_status(cfg)
    check(f"chunk #{mid} không còn done sau khi invalidate TTS", not st2.chunks[mid].done)
    check(f"chunk #{mid}: next_stage_label = 'Tạo giọng đọc' (đúng bước còn dở)", st2.chunks[mid].next_stage_label == "Tạo giọng đọc")
    check("các chunk KHÁC vẫn done bình thường (không bị ảnh hưởng)",
          all(c.done for i, c in enumerate(st2.chunks) if i != mid))
    check("all_tts_ready = False (còn 1 chunk thiếu TTS)", not st2.all_tts_ready)
    check("all_scripts_ready vẫn True (script vẫn còn nguyên, chỉ mất TTS)", st2.all_scripts_ready)
    check(f"summary_line nêu đúng chunk #{mid + 1} và đúng bước dở dang",
          f"#{mid + 1}" in st2.summary_line() and "Tạo giọng đọc" in st2.summary_line())

    # 4) chạy lại (mô phỏng người dùng bấm nút "Ghép & xuất video" sau khi thấy trạng thái trên)
    n_req_before = len(srv.requests)
    app.run_pipeline(cfg)
    check("resume: KHÔNG gọi lại Gemini cho phần đã có kịch bản", len(srv.requests) == n_req_before)
    st3 = pipeline_status.read_status(cfg)
    check("sau khi resume: tất cả chunk lại done hết", st3.n_done == st3.n_total)

    # 5) đổi cấu hình (fingerprint khác) → read_status() phải trả None, không lẫn trạng thái cũ
    cfg2 = web_cfg(tmp, srv, video, "out_status", chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0,
                   language="en")
    st4 = pipeline_status.read_status(cfg2)
    check("đổi ngôn ngữ (fingerprint khác) → read_status() = None, không dùng nhầm trạng thái cũ", st4 is None)

srv.close()
print("TẤT CẢ PASS ✔")
