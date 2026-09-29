"""background_jobs.py — chạy pipeline trong LUỒNG NỀN THỰC SỰ, độc lập với vòng đời thực thi của Streamlit.

Vì sao cần: Streamlit chạy lại (rerun) TOÀN BỘ script mỗi khi có tương tác trên trang (bấm bất kỳ nút nào,
đổi bất kỳ ô nào, hoặc trình duyệt kết nối lại) — và khi rerun xảy ra trong lúc script CŨ đang chạy dở,
Streamlit HUỶ NGANG luồng đang chạy, kể cả khi nó đang gọi `long_video_pipeline.run()` giữa chừng. Đây là
hành vi tiêu chuẩn của Streamlit với MỌI app chạy tác vụ dài trong callback nút bấm, không phải lỗi riêng
của app này. Bằng chứng thực tế: "Lần chạy bị NGẮT giữa chừng bởi StopException" xảy ra nhiều lần trong
một phiên dù người dùng không cố ý bấm gì thêm — nhiều khả năng do máy tạm ngủ/mạng chập chờn khiến trình
duyệt tự kết nối lại, và Streamlit coi đó là một tương tác mới.

Cách khắc phục: đưa `long_video_pipeline.run()` ra LUỒNG NỀN riêng (threading.Thread), lưu trong một SỔ
ĐĂNG KÝ sống NGOÀI vòng đời của bất kỳ lần rerun Streamlit nào (biến cấp module, không phải st.session_state
— session_state cũng bị/reset theo những cách khác nhau tuỳ phiên bản Streamlit, còn biến module thì chắc
chắn sống suốt vòng đời tiến trình .exe). Từ đó: bấm nút chỉ KHỞI ĐỘNG luồng nền rồi trả về ngay (không
chặn), nên Streamlit rerun bao nhiêu lần cũng không đụng tới luồng đang chạy thật; trang chỉ ĐỌC trạng thái
(đã lưu trong sổ đăng ký) để hiển thị, không điều khiển vòng đời của nó.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("svvc.bgjob")


@dataclass
class JobStatus:
    workdir: Path
    stop_after: int | None
    state: str = "running"              # "running" | "done" | "error" | "cancelled"
    current_step: str = ""
    chunk_progress: tuple[int, int] | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    @property
    def elapsed_sec(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


_REGISTRY: dict[str, JobStatus] = {}
_LOCK = threading.Lock()


def _key(workdir: Path) -> str:
    return str(Path(workdir).resolve())


def get_job(workdir: Path) -> JobStatus | None:
    with _LOCK:
        return _REGISTRY.get(_key(workdir))


def is_running(workdir: Path) -> bool:
    job = get_job(workdir)
    return job is not None and job.state == "running" and job.thread is not None and job.thread.is_alive()


def start_job(workdir: Path, run_fn: Callable[[JobStatus], dict], *, stop_after: int | None) -> JobStatus:
    """`run_fn(job)`: hàm THỰC SỰ chạy pipeline ở luồng nền, nhận `job` để tự cập nhật `job.current_step` /
    `job.chunk_progress` qua callback và kiểm tra `job.cancel_event` để dừng sớm khi được yêu cầu. Ném
    `RuntimeError` ngay (không tạo job mới) nếu đã có job khác đang chạy cho ĐÚNG workdir này."""
    if is_running(workdir):
        raise RuntimeError("Đã có một tiến trình khác đang chạy nền cho video này.")
    job = JobStatus(workdir=Path(workdir), stop_after=stop_after)

    def _target() -> None:
        try:
            job.result = run_fn(job)
            job.state = "cancelled" if job.cancel_event.is_set() else "done"
        except Exception as e:  # noqa: BLE001 — luồng nền: PHẢI tự bắt lỗi ở đây, không còn ai "phía trên" bắt hộ nữa
            if job.cancel_event.is_set():
                # `run_fn` (long_video_pipeline.run) ném PipelineCancelled khi thấy cancel_event đã được set —
                # đây LÀ Exception về mặt kỹ thuật nhưng KHÔNG PHẢI lỗi thật, chỉ là cách dừng có chủ đích.
                # Không cần biết cụ thể loại exception là gì (tránh phải import PipelineCancelled vào module
                # này, giữ background_jobs.py độc lập, không phụ thuộc riêng vào pipeline nào) — chỉ cần biết
                # ĐÃ CÓ yêu cầu dừng là đủ để phân biệt với lỗi thật.
                job.state = "cancelled"
            else:
                job.state = "error"
                job.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                log.exception("Luồng nền chạy pipeline gặp lỗi")
        finally:
            job.finished_at = time.time()

    t = threading.Thread(target=_target, daemon=True, name=f"pipeline-{_key(workdir)[-12:]}")
    job.thread = t
    with _LOCK:
        _REGISTRY[_key(workdir)] = job
    t.start()
    return job


def request_cancel(workdir: Path) -> bool:
    """Yêu cầu dừng job đang chạy — CHỈ dừng được ở các điểm kiểm tra an toàn (ranh giới giữa 2 chunk, xem
    `long_video_pipeline.py`), không dừng ngay lập tức giữa chừng một thao tác (vd giữa lúc FFmpeg đang mã
    hoá). Trả về False nếu không có job nào đang chạy cho workdir này."""
    job = get_job(workdir)
    if job is None or job.state != "running":
        return False
    job.cancel_event.set()
    return True


def clear_job(workdir: Path) -> None:
    """Xoá job đã XONG (done/error/cancelled) khỏi sổ đăng ký — KHÔNG xoá job đang chạy. Dùng để dọn sổ
    đăng ký trước khi bắt đầu một job mới cho cùng workdir, tránh hiển thị nhầm trạng thái/lỗi của lần trước."""
    with _LOCK:
        job = _REGISTRY.get(_key(workdir))
        if job is not None and job.state != "running":
            del _REGISTRY[_key(workdir)]
