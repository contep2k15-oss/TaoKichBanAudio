"""python tests/test_background_jobs.py — sổ đăng ký luồng nền phải sống sót qua nhiều lần "Streamlit rerun"
giả lập (tạo đối tượng script mới, gọi lại các hàm y hệt cách app_streamlit.py sẽ làm mỗi lần rerun)."""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import background_jobs as bg  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


with tempfile.TemporaryDirectory() as d:
    workdir = Path(d) / "wd1"

    # ── 1) chạy 1 job chậm, đọc trạng thái NHIỀU LẦN như Streamlit rerun liên tục — job KHÔNG bị ảnh hưởng ──
    events = []

    def slow_run(job: bg.JobStatus) -> dict:
        for i in range(5):
            if job.cancel_event.is_set():
                return {"video": None}
            job.current_step = f"bước {i + 1}/5"
            job.chunk_progress = (i + 1, 5)
            events.append(i)
            time.sleep(0.15)
        return {"video": "ket_qua.mp4"}

    job = bg.start_job(workdir, slow_run, stop_after=None)
    check("job bắt đầu ở trạng thái 'running'", job.state == "running")

    # mô phỏng 20 LẦN "Streamlit rerun" liên tiếp trong lúc job đang chạy — mỗi lần chỉ ĐỌC, không đụng tới job
    for _ in range(20):
        j = bg.get_job(workdir)          # y hệt việc app_streamlit.py đọc lại trạng thái mỗi lần rerun
        assert j is job                  # PHẢI là CÙNG MỘT object — không bị tạo lại/mất
        time.sleep(0.02)
    check("sau 20 lần 'rerun' giả lập, job vẫn là CÙNG một đối tượng, không bị mất/reset", True)

    job.thread.join(timeout=5)
    check(f"job chạy xong đầy đủ cả 5 bước (không bị cắt ngang): events={events}", events == [0, 1, 2, 3, 4])
    check("trạng thái cuối = 'done'", job.state == "done")
    check("kết quả được lưu lại đúng", job.result == {"video": "ket_qua.mp4"})
    check("is_running() = False sau khi xong", not bg.is_running(workdir))

    # ── 2) KHÔNG cho khởi động job THỨ HAI trong khi job cũ (khác workdir không liên quan) — kiểm tra cô lập theo workdir ──
    wd2 = Path(d) / "wd2"
    ev2 = []

    def instant(job):
        ev2.append(1)
        return {"video": "x2.mp4"}
    job2 = bg.start_job(wd2, instant, stop_after=2)
    job2.thread.join(timeout=5)
    check("job ở workdir KHÁC chạy độc lập, không bị chặn bởi job đã xong ở workdir 1", ev2 == [1])

    # ── 3) KHÔNG cho khởi động 2 job CÙNG workdir trong khi 1 job đang chạy (đúng tình huống bấm 2 nút liền) ──
    wd3 = Path(d) / "wd3"
    started = threading.Event()

    def blocking(job):
        started.set()
        time.sleep(0.5)
        return {"video": "x3.mp4"}
    jobA = bg.start_job(wd3, blocking, stop_after=None)
    started.wait(timeout=2)
    try:
        bg.start_job(wd3, blocking, stop_after=None)
        raise AssertionError("phải từ chối, không cho chạy chồng job")
    except RuntimeError as e:
        check(f"từ chối khởi động job thứ 2 khi job cũ CÙNG workdir vẫn đang chạy: {e}", True)
    jobA.thread.join(timeout=5)
    check("job đầu vẫn hoàn tất bình thường dù có lần thử khởi động chồng", jobA.state == "done")

    # ── 4) YÊU CẦU DỪNG (cancel) — job phải dừng SỚM ở điểm kiểm tra, không chạy hết ──
    wd4 = Path(d) / "wd4"
    n_steps_done = []

    def cancellable(job):
        for i in range(10):
            if job.cancel_event.is_set():
                return {"video": None, "cancelled_at": i}
            n_steps_done.append(i)
            time.sleep(0.1)
        return {"video": "khong_nen_toi_day.mp4"}
    job4 = bg.start_job(wd4, cancellable, stop_after=None)
    time.sleep(0.25)                      # để job chạy vài bước trước khi dừng
    check("request_cancel() thành công khi job đang chạy", bg.request_cancel(wd4))
    job4.thread.join(timeout=5)
    check(f"job dừng SỚM (chưa chạy hết 10 bước): đã chạy {len(n_steps_done)} bước", len(n_steps_done) < 10)
    check("trạng thái cuối = 'cancelled' (không phải 'done')", job4.state == "cancelled")
    check("request_cancel() lần 2 (đã xong) trả về False", not bg.request_cancel(wd4))

    # ── 5) job LỖI: bắt gọn, không làm crash luồng nền, ghi rõ traceback ──
    wd5 = Path(d) / "wd5"

    def broken(job):
        raise ValueError("lỗi giả lập trong luồng nền")
    job5 = bg.start_job(wd5, broken, stop_after=None)
    job5.thread.join(timeout=5)
    check("job lỗi: trạng thái = 'error', không làm crash gì khác", job5.state == "error")
    check("có traceback đầy đủ để debug", "ValueError" in job5.error and "lỗi giả lập" in job5.error)

    # ── 6) clear_job: chỉ xoá job ĐÃ XONG, không đụng job đang chạy ──
    wd6 = Path(d) / "wd6"
    gate = threading.Event()

    def waits(job):
        gate.wait(timeout=5)
        return {"video": "x6.mp4"}
    job6 = bg.start_job(wd6, waits, stop_after=None)
    bg.clear_job(wd6)                     # đang chạy → KHÔNG được xoá
    check("clear_job() không xoá job đang chạy", bg.get_job(wd6) is job6)
    gate.set()
    job6.thread.join(timeout=5)
    bg.clear_job(wd6)                     # giờ đã xong → xoá được
    check("clear_job() xoá được job đã xong", bg.get_job(wd6) is None)

# ── 7) job NÉM EXCEPTION vì đã bị yêu cầu dừng (đúng cách long_video_pipeline.run ném PipelineCancelled) →
#    PHẢI nhận đúng thành 'cancelled', KHÔNG PHẢI 'error' (lỗi thật đã tìm thấy và sửa khi test app_streamlit.py) ──
wd7 = Path(d) / "wd7"

def raises_when_cancelled(job):
    time.sleep(0.1)
    if job.cancel_event.is_set():
        raise RuntimeError("dừng theo yêu cầu (mô phỏng PipelineCancelled — CŨNG LÀ một Exception)")
    return {"video": "khong_toi_day.mp4"}

job7 = bg.start_job(wd7, raises_when_cancelled, stop_after=None)
bg.request_cancel(wd7)
job7.thread.join(timeout=5)
check(f"NÉM EXCEPTION sau khi đã cancel → nhận đúng là 'cancelled', KHÔNG PHẢI 'error' (state={job7.state})",
      job7.state == "cancelled")
check("không có error message (vì không coi là lỗi thật)", job7.error is None)

# ── 8) NÉM EXCEPTION mà KHÔNG hề cancel trước → vẫn phải là 'error' như bình thường (không bị lẫn lộn) ──
wd8 = Path(d) / "wd8"

def raises_without_cancel(job):
    raise ValueError("lỗi thật, không liên quan gì tới cancel")

job8 = bg.start_job(wd8, raises_without_cancel, stop_after=None)
job8.thread.join(timeout=5)
check(f"lỗi thật (không cancel) vẫn đúng là 'error' (state={job8.state})", job8.state == "error")
check("có error message đầy đủ", job8.error and "ValueError" in job8.error)

print("TẤT CẢ PASS ✔")
