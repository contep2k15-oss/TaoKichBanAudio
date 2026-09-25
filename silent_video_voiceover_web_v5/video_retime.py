"""LAST-RESORT (mặc định TẮT, bật bằng --allow-video-retime): khi một đoạn thuyết minh vẫn dài hơn cả
khoảng trống cho phép DÙ ĐÃ tăng tốc audio tối đa 1.25×, thay vì cắt cụt lời thoại, module này làm CHẬM
khung hình VIDEO trong đúng cửa sổ đó lại một chút để có thêm chỗ chứa — đúng như đề bài yêu cầu: "tự điều
chỉnh tốc độ khung hình/video tương ứng".

Cách làm: dựng MỘT filter_complex FFmpeg duy nhất trên MỘT input video (không phải nhiều input rời rạc,
nên không gặp vấn đề "WinError 206 / câu lệnh quá dài" mà đề bài cảnh báo — vấn đề đó chỉ xảy ra khi có
NHIỀU `-i` cho nhiều FILE, còn đây một input, một đồ thị filter): chia video thành các đoạn xen kẽ
[giữ nguyên][làm chậm][giữ nguyên]...[làm chậm][giữ nguyên], mỗi đoạn `trim` + `setpts`, rồi `concat` lại.

Vì video bị kéo dài thêm trong các cửa sổ retime, mọi mốc thời gian TỪ ĐIỂM ĐÓ TRỞ ĐI trong video bị dịch
chuyển — `build_time_remap()` trả về một hàm ánh xạ mốc-thời-gian-gốc → mốc-thời-gian-mới, dùng để cập
nhật lại vị trí các đoạn thuyết minh CÒN LẠI trước khi ghép audio (audio_assembler.py) và mux (video_muxer.py).

Vì đây là thao tác NẶNG (phải re-encode phần video liên quan) và chỉ nên là phương án cuối cùng cho một số
ít đoạn hiếm gặp (không phải cho mọi chunk), tính năng này mặc định TẮT.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from utils import MediaToolError, PipelineError, run_cmd

log = logging.getLogger("svvc.retime")

MIN_GAP_BETWEEN_WINDOWS_SEC = 0.05   # gộp 2 cửa sổ retime nếu quá sát nhau, tránh đoạn 'giữ nguyên' dài 0s


@dataclass(frozen=True)
class RetimeWindow:
    start_sec: float
    end_sec: float
    factor: float        # >1.0: LÀM CHẬM (video trong cửa sổ này sẽ dài ra factor lần)

    def __post_init__(self) -> None:
        if self.end_sec <= self.start_sec:
            raise ValueError("RetimeWindow: end_sec phải > start_sec")
        if self.factor <= 1.0:
            raise ValueError("RetimeWindow: factor phải > 1.0 (chỉ hỗ trợ làm CHẬM, không làm nhanh video)")


def _merge_windows(windows: list[RetimeWindow]) -> list[RetimeWindow]:
    out: list[RetimeWindow] = []
    for w in sorted(windows, key=lambda w: w.start_sec):
        if out and w.start_sec - out[-1].end_sec < MIN_GAP_BETWEEN_WINDOWS_SEC:
            prev = out.pop()
            # gộp hai cửa sổ liền nhau, lấy factor lớn hơn (an toàn: đủ chỗ cho cả hai nhu cầu)
            out.append(RetimeWindow(prev.start_sec, max(prev.end_sec, w.end_sec), max(prev.factor, w.factor)))
        else:
            out.append(w)
    return out


def build_time_remap(duration_sec: float, windows: list[RetimeWindow]) -> Callable[[float], float]:
    """Trả về hàm remap(t_goc) -> t_moi. Ngoài các cửa sổ retime, remap là ánh xạ đồng nhất (chỉ dịch theo
    tổng độ dài đã được chèn thêm bởi các cửa sổ retime nằm TRƯỚC đó)."""
    windows = _merge_windows(windows)
    for w in windows:
        if not (0 <= w.start_sec < w.end_sec <= duration_sec):
            raise ValueError(f"RetimeWindow {w} nằm ngoài phạm vi video [0, {duration_sec}]")

    # breakpoints: (t_goc_start, t_moi_start, factor_trong_doan_nay, t_goc_end)
    breakpoints: list[tuple[float, float, float, float]] = []
    t_new = 0.0
    t_old = 0.0
    for w in windows:
        if w.start_sec > t_old:                                  # đoạn giữ nguyên trước cửa sổ retime
            breakpoints.append((t_old, t_new, 1.0, w.start_sec))
            t_new += w.start_sec - t_old
        breakpoints.append((w.start_sec, t_new, w.factor, w.end_sec))
        t_new += (w.end_sec - w.start_sec) * w.factor
        t_old = w.end_sec
    if t_old < duration_sec:
        breakpoints.append((t_old, t_new, 1.0, duration_sec))

    def remap(t: float) -> float:
        t = max(0.0, min(t, duration_sec))
        for old_start, new_start, factor, old_end in breakpoints:
            if old_start <= t <= old_end:
                return new_start + (t - old_start) * factor
        return t   # không tới đây nếu breakpoints phủ kín [0, duration_sec]

    return remap


def total_new_duration(duration_sec: float, windows: list[RetimeWindow]) -> float:
    return duration_sec + sum((w.end_sec - w.start_sec) * (w.factor - 1.0) for w in _merge_windows(windows))


# ════════════════════════════════════════════════════════════
# Dựng video đã retime bằng FFmpeg (một input, một filter_complex)
# ════════════════════════════════════════════════════════════
def build_retimed_video(video: Path, duration_sec: float, windows: list[RetimeWindow], out_path: Path,
                        timeout: float = 3600.0) -> None:
    """Ghi ra `out_path` bản video đã làm chậm đúng các `windows`, giữ nguyên tốc độ ở phần còn lại.
    Video output KHÔNG có audio (audio được ghép riêng ở audio_assembler/video_muxer theo mốc thời gian MỚI,
    tính qua `build_time_remap()`), nên filter chỉ áp dụng cho luồng hình."""
    windows = _merge_windows(windows)
    if not windows:
        raise PipelineError("build_retimed_video: danh sách windows rỗng.")

    segments: list[tuple[float, float, float]] = []      # (start, end, factor) liền mạch, phủ kín [0, duration]
    t = 0.0
    for w in windows:
        if w.start_sec > t:
            segments.append((t, w.start_sec, 1.0))
        segments.append((w.start_sec, w.end_sec, w.factor))
        t = w.end_sec
    if t < duration_sec:
        segments.append((t, duration_sec, 1.0))

    labels = []
    graph_parts = []
    for i, (s, e, factor) in enumerate(segments):
        lbl = f"v{i}"
        graph_parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts={factor:.6f}*(PTS-STARTPTS)[{lbl}]")
        labels.append(f"[{lbl}]")
    graph_parts.append("".join(labels) + f"concat=n={len(segments)}:v=1:a=0[vout]")
    filter_complex = ";".join(graph_parts)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("Dựng lại video với %d cửa sổ làm chậm (retime) — bước này re-encode nên hơi chậm...", len(windows))
    run_cmd(["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-filter_complex", filter_complex, "-map", "[vout]",
             "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", out_path], "video_retime",
            timeout=timeout)
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise MediaToolError("FFmpeg không tạo được video đã retime.")
