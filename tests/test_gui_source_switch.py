"""python tests/test_gui_source_switch.py — đổi qua lại giữa "link YouTube" và "Tải video lên" không được sập
trang và cấu hình gửi cho pipeline phải THEO CHẾ ĐỘ ĐANG CHỌN (không dính giá trị cũ của chế độ kia)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from streamlit.testing.v1 import AppTest  # noqa: E402

import long_video_pipeline  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


captured: list = []


def fake_run(cfg, **kw):
    captured.append(cfg)
    return {"script": None, "voiceover": None, "video": None}


long_video_pipeline.run = fake_run
LINK = "https://www.youtube.com/watch?v=tZif80BA6hI"

with tempfile.TemporaryDirectory() as d:
    video = Path(d) / "clip.mp4"
    video.write_bytes(b"x" * 1000)                 # chỉ cần tồn tại; pipeline giả không đọc nội dung

    # ── (1) KỊCH BẢN CỦA BẠN: đang ở chế độ link (đã nhập link) → đổi sang tải lên, chọn file, tick highlight ──
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app_streamlit.py"), default_timeout=30).run()
    at.session_state["source_mode"] = "youtube"
    at.session_state["youtube_url"] = LINK
    at.run()
    check("chế độ link, có link: không lỗi", not list(at.exception))

    # đổi chế độ + chọn file + tick highlight TRONG CÙNG MỘT LẦN CHẠY (đúng lúc trước đây sập)
    at.session_state["source_mode"] = "upload"
    at.session_state["video_path"] = str(video)
    at.session_state["highlight_mode"] = True
    at.run()
    check("đổi sang tải lên + tick highlight: KHÔNG sập trang", not list(at.exception))
    check("không hiện thông báo 'chưa hỗ trợ' nhầm (đang ở chế độ tải lên)",
          not any("Chưa hỗ trợ" in e.value for e in at.error) and not any("Chưa dùng được" in w.value for w in at.warning))
    btn = next(b for b in at.button if b.label == "④ Hoàn tất")
    check("nút chạy được BẬT", not btn.disabled)
    btn.click().run()
    check("bấm chạy: pipeline nhận đúng 1 cấu hình", len(captured) == 1)
    cfg = captured[-1]
    check("cấu hình: highlight_mode=True (giữ cảnh hay hoạt động ở chế độ tải lên)", cfg.highlight_mode is True)
    check("cấu hình: KHÔNG dính link YouTube cũ", cfg.youtube_url is None)
    check("cấu hình: dùng đúng file đã chọn", cfg.video_path == video.resolve())

    # ── (2) chiều ngược lại: đang ở tải lên (có file) → đổi sang link: KHÔNG dính file cũ, highlight bị chặn rõ ràng ──
    at.session_state["source_mode"] = "youtube"
    at.session_state["youtube_url"] = LINK
    at.run()
    check("đổi sang link (đang tick highlight): không sập", not list(at.exception))
    check("hiện rõ thông báo chưa hỗ trợ + khoá nút chạy",
          any("Chưa hỗ trợ" in e.value for e in at.error) and next(b for b in at.button if b.label == "④ Hoàn tất").disabled)

    # ── (3) bỏ tick → chạy bằng link: cấu hình chỉ có link, không dính file cũ ──
    at.session_state["highlight_mode"] = False
    at.run()
    n = len(captured)
    next(b for b in at.button if b.label == "④ Hoàn tất").click().run()
    cfg = captured[-1]
    check("chạy bằng link: pipeline nhận cấu hình mới", len(captured) == n + 1)
    check("cấu hình: có link, KHÔNG dính file tải lên cũ", cfg.youtube_url == LINK and cfg.video_path is None)
    check("cấu hình: highlight tắt", cfg.highlight_mode is False)

print("TẤT CẢ PASS ✔")
