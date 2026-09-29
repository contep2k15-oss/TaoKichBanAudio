"""python tests/test_gui_background_run.py — nút chạy KHÔNG chặn trang (background_jobs), thấy trạng thái
đang chạy/đã dừng/tiếp tục/hoàn tất đúng qua nhiều lần rerun — TÁI HIỆN đúng kịch bản người dùng gặp
("bấm nút trong lúc đang chạy" không còn làm gãy tiến trình vì tiến trình không còn chạy TRONG script nữa)."""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from streamlit.testing.v1 import AppTest  # noqa: E402

import background_jobs as bg  # noqa: E402
import long_video_pipeline  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


APP = str(Path(__file__).resolve().parent.parent / "app_streamlit.py")
gate = threading.Event()          # điều khiển pipeline giả lập dừng ở đúng chỗ mình muốn để kiểm tra trạng thái
skip_gate = threading.Event()     # bật lên khi KHÔNG cần chạy chậm nữa (đã kiểm xong các trạng thái cần xem giữa chừng)
call_log = []


def fake_run(cfg, *, on_step=None, on_chunk_progress=None, interactive=False, cancel_event=None):
    call_log.append("start")
    if on_step:
        on_step(2, "Tạo kịch bản (giả lập)")
    for i in range(1, 4):
        if cancel_event is not None and cancel_event.is_set():
            call_log.append(f"cancelled_at_{i}")
            from utils import PipelineCancelled
            raise PipelineCancelled("dừng theo yêu cầu (giả lập)")
        if on_chunk_progress:
            on_chunk_progress(i, 3)
        if not skip_gate.is_set():
            gate.wait(timeout=5)      # CHỜ tín hiệu — mô phỏng "đang chạy dở", đủ lâu để test kịp đọc trạng thái
            gate.clear()
    call_log.append("finished")
    out_video = Path(cfg.output_dir) / "output_dubbed_video.mp4"
    out_video.parent.mkdir(parents=True, exist_ok=True)
    out_video.write_bytes(b"fake mp4 bytes")   # phải TỒN TẠI THẬT trên đĩa để nhánh hiển thị video của GUI chạy tới
    return {"script": None, "voiceover": None, "video": str(out_video)}


long_video_pipeline.run = fake_run

with tempfile.TemporaryDirectory() as d:
    video = Path(d) / "clip.mp4"
    video.write_bytes(b"x" * 1000)

    at = AppTest.from_file(APP, default_timeout=30).run()
    at.session_state["video_path"] = str(video)
    at.session_state["output_dir"] = str(Path(d) / "out")
    at.run()

    # ── 1) bấm "④ Hoàn tất" → PHẢI trả về NGAY (không chặn), thấy trạng thái "đang chạy nền" ──
    btn = next(b for b in at.button if b.label == "④ Hoàn tất")
    t0 = time.perf_counter()
    btn.click().run()
    dt = time.perf_counter() - t0
    check(f"bấm nút chạy TRẢ VỀ NGAY, không chặn trang (mất {dt:.2f}s, KHÔNG phải chờ hết cả pipeline)", dt < 2.0)
    check("KHÔNG có exception nào", not list(at.exception))
    infos = [i.value for i in at.info]
    check(f"thấy trạng thái 'Đang chạy nền': {infos}", any("Đang chạy" in x for x in infos))

    # ── 2) "rerun" NHIỀU LẦN trong lúc job đang chạy dở (mô phỏng đúng việc bấm nút/thao tác khác) —
    #     job KHÔNG được reset/mất tiến độ, vẫn là CÙNG một job nền ──
    workdir = Path(at.session_state["output_dir"]).resolve() / "work"
    job_before = bg.get_job(workdir)
    for _ in range(15):
        at.run()                     # y hệt việc trang bị rerun bởi thao tác khác trên giao diện
    check("KHÔNG có exception sau 15 lần rerun liên tiếp trong lúc job đang chạy", not list(at.exception))
    check("vẫn là CÙNG MỘT job nền (không bị tạo lại/mất)", bg.get_job(workdir) is job_before)
    check("job vẫn đang 'running' sau 15 lần rerun — KHÔNG bị StopException như trước đây", job_before.state == "running")

    # ── 3) bấm "⏹ Dừng" → gửi yêu cầu, job dừng ở ranh giới an toàn ──
    btn_dung = next((b for b in at.button if "Dừng" in b.label), None)
    check("có nút Dừng khi đang chạy", btn_dung is not None)
    btn_dung.click().run()
    gate.set()                       # để job giả lập kịp nhận ra cancel_event tại vòng lặp kế
    job_before.thread.join(timeout=5)
    check(f"job dừng đúng: trạng thái cuối = 'cancelled' (call_log={call_log})", job_before.state == "cancelled")
    at.run()
    infos = [w.value for w in at.warning]
    check(f"trang hiện đúng thông báo 'đã dừng theo yêu cầu': {infos}", any("dừng theo yêu cầu" in x for x in infos))

    # ── 4) bấm "▶️ Tiếp tục" → khởi động job MỚI, tiếp tục đúng cấu hình cũ (stop_after giữ nguyên) ──
    call_log.clear()
    skip_gate.set()   # không cần chạy chậm/dừng giữa chừng nữa — để job lần này chạy thẳng một mạch tới xong
    btn_tieptuc = next(b for b in at.button if "Tiếp tục" in b.label)
    btn_tieptuc.click().run()
    job2 = bg.get_job(workdir)
    check("job mới KHÁC job cũ đã dừng", job2 is not job_before)
    job2.thread.join(timeout=5)
    check(f"bấm Tiếp tục: job MỚI đã chạy (call_log={call_log})", "start" in call_log)
    check(f"job mới chạy XONG HẲN (không bị dừng): call_log={call_log}", "finished" in call_log)
    check("trạng thái cuối = 'done'", job2.state == "done")

    # ── 5) trang hiện đúng kết quả sau khi job xong ──
    at.run()
    check("KHÔNG có exception khi hiện kết quả", not list(at.exception))
    dl_labels = [b.label for b in at.download_button]
    check(f"hiện nút tải video kết quả: {dl_labels}", any("video" in x.lower() for x in dl_labels))

print("TẤT CẢ PASS ✔")
