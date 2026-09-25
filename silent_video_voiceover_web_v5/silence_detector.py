"""Phát hiện khoảng lặng (silence gap) theo kiểu STREAMING — không load âm thanh vào RAM.

Thay vì dùng `pydub.AudioSegment.from_file()` (giải mã và giữ TOÀN BỘ audio trong RAM dưới dạng mảng —
với video 60 phút stereo 44.1kHz là hơn 600 MB, và với video hàng giờ có thể làm tràn RAM), hàm dưới đây
chạy FFmpeg với filter `silencedetect` ở chế độ "phân tích rồi bỏ" (`-f null -`): FFmpeg tự giải mã và xử
lý audio theo từng khối nhỏ trong tiến trình CON, Python chỉ đọc TỪNG DÒNG log ở stderr (streaming) để lấy
mốc thời gian — bộ nhớ Python dùng cho bước này gần như hằng số, không phụ thuộc độ dài video.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

from utils import MediaToolError

log = logging.getLogger("svvc.silence")

_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)")


def detect_silences(video: Path, *, has_audio: bool, min_silence_sec: float = 0.6, noise_db: float = -30.0,
                    timeout: float | None = None) -> list[tuple[float, float]]:
    """Trả về danh sách (start_sec, end_sec) các khoảng lặng trong toàn bộ audio của video.

    Chạy MỘT lượt FFmpeg duy nhất, đọc stderr theo dòng (streaming) — không giữ audio đã giải mã trong RAM.
    Video không có audio → trả về [] ngay (không có "khoảng lặng" nào để neo, bộ chia đoạn sẽ tự dùng
    scene-cut hoặc cắt cứng thay thế — xem chunk_planner.py).
    """
    if not has_audio:
        log.info("Video không có track âm thanh → bỏ qua phát hiện khoảng lặng.")
        return []

    cmd = ["ffmpeg", "-nostdin", "-i", str(video), "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
           "-f", "null", "-"]
    log.info("Quét khoảng lặng (streaming, noise=%.0fdB, min=%.2fs)...", noise_db, min_silence_sec)
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", creationflags=creationflags)
    except FileNotFoundError as e:
        raise MediaToolError("Không tìm thấy 'ffmpeg' trong PATH.") from e

    silences: list[tuple[float, float]] = []
    pending_start: float | None = None
    assert proc.stderr is not None
    try:
        for line in proc.stderr:                      # đọc TỪNG DÒNG khi FFmpeg ghi ra — không đợi toàn bộ
            m = _START_RE.search(line)
            if m:
                pending_start = float(m.group(1))
                continue
            m = _END_RE.search(line)
            if m and pending_start is not None:
                end = float(m.group(1))
                silences.append((max(0.0, pending_start), end))
                pending_start = None
    finally:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise MediaToolError(f"Quét khoảng lặng quá thời gian chờ ({timeout}s).") from None

    if proc.returncode not in (0,):
        # silencedetect vẫn ghi log ra stderr dù thành công (đó là cách filter này báo kết quả), nên chỉ
        # coi là lỗi khi exit code khác 0 (ffmpeg crash / không đọc được file).
        raise MediaToolError(f"FFmpeg quét khoảng lặng thất bại (exit {proc.returncode}). Kiểm tra file video có hỏng không.")

    log.info("Phát hiện %d khoảng lặng.", len(silences))
    return silences
