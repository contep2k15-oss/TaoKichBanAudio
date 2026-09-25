"""Trích xuất frame bằng OpenCV (theo khoảng đều hoặc theo phân cảnh) và đóng dấu timestamp lên từng ảnh.

Dấu timestamp (góc trên-trái) giúp Gemini biết chính xác mỗi ảnh ứng với thời điểm nào, kể cả khi giao diện web
không cho xen kẽ chữ giữa các ảnh đính kèm.

Với VIDEO DÀI, `extract_samples_in_range()` chỉ trích frame trong PHẠM VI MỘT MACRO-CHUNK (xem
chunk_planner.py) — mỗi chunk ghi vào thư mục con riêng `frames/chunk_XXXX/` và không đụng tới frame của
các chunk khác, để (a) một chunk xử lý xong có thể giải phóng ảnh của nó mà không ảnh hưởng chunk kế tiếp,
và (b) resume một chunk giữa chừng không xoá mất frame của các chunk đã xong trước đó (khác với
`extract_samples()` — hàm gốc dùng cho toàn bộ video ngắn — vốn xoá sạch `frames/` mỗi lần chạy).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config
from utils import MediaInfo, MediaToolError, PipelineError, format_timestamp

log = logging.getLogger("svvc.video")


@dataclass
class FrameSample:
    index: int               # thứ tự toàn cục (1-based)
    timestamp_sec: float
    path: Path
    window_start: float      # cửa sổ thời gian mà frame này đại diện
    window_end: float


# ════════════════════════════════════════════════════════════
# Phát hiện phân cảnh bằng OpenCV (histogram HSV)
# ════════════════════════════════════════════════════════════
def detect_scene_cuts(video: Path, media: MediaInfo, threshold: float, min_gap_sec: float,
                      sample_fps: float = 4.0) -> list[float]:
    """Trả về danh sách thời điểm (giây) có cảnh cắt. So sánh histogram HSV (3 kênh, có cả độ sáng) của các frame lấy mẫu ~4 fps
    bằng khoảng cách Bhattacharyya (0 = giống hệt, 1 = khác hoàn toàn)."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise MediaToolError(f"OpenCV không mở được video: {video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or media.fps
    step = max(1, round(fps / sample_fps))
    cuts: list[float] = []
    prev = None
    last_cut = -1e9
    idx = 0
    try:
        while cap.grab():
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    hsv = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2HSV)
                    hist = cv2.calcHist([hsv], [0, 1, 2], None, [12, 4, 8], [0, 180, 0, 256, 0, 256])
                    cv2.normalize(hist, hist, 1.0, 0.0, cv2.NORM_L1)
                    t = idx / fps
                    if prev is not None:
                        dist = cv2.compareHist(prev, hist, cv2.HISTCMP_BHATTACHARYYA)
                        if dist > threshold and t - last_cut >= min_gap_sec:
                            cuts.append(round(t, 3))
                            last_cut = t
                            log.debug("Cảnh cắt tại %s (khoảng cách %.2f)", format_timestamp(t), dist)
                    prev = hist
            idx += 1
    finally:
        cap.release()
    return cuts


# ════════════════════════════════════════════════════════════
# Chia cửa sổ thời gian
# ════════════════════════════════════════════════════════════
def _equal_split(start: float, end: float, n: int) -> list[tuple[float, float]]:
    step = (end - start) / n
    return [(start + i * step, start + (i + 1) * step) for i in range(n)]


def _merge_short(scenes: list[tuple[float, float]], min_len: float) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for s, e in scenes:
        if merged and (e - s) < min_len:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    if len(merged) > 1 and (merged[0][1] - merged[0][0]) < min_len:
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    return merged


def build_time_windows(duration: float, cfg: Config, cuts: list[float] | None) -> list[tuple[float, float]]:
    interval = cfg.frame_interval_sec
    if cfg.extract_mode == "scene" and cuts:
        bounds = [0.0] + [c for c in cuts if 0 < c < duration] + [duration]
        scenes = [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]
        windows: list[tuple[float, float]] = []
        for s, e in _merge_short(scenes, cfg.min_scene_sec):
            if e - s > cfg.max_scene_sec:
                windows += _equal_split(s, e, max(2, round((e - s) / interval)))
            else:
                windows.append((s, e))
    else:
        if cfg.extract_mode == "scene":
            log.info("Không phát hiện cảnh cắt → chia theo khoảng đều.")
        windows = _equal_split(0.0, duration, max(1, round(duration / interval)))

    if len(windows) > cfg.max_frames:
        log.warning("%d cửa sổ > giới hạn %d → nới khoảng lấy mẫu lên %.2fs.", len(windows), cfg.max_frames,
                    duration / cfg.max_frames)
        windows = _equal_split(0.0, duration, cfg.max_frames)
    return windows


def frame_times(start: float, end: float, cfg: Config) -> list[float]:
    k = min(cfg.max_frames_per_window, max(1, round((end - start) / cfg.frame_interval_sec)))
    return [start + (j + 0.5) * (end - start) / k for j in range(k)]


# ════════════════════════════════════════════════════════════
# Trích frame
# ════════════════════════════════════════════════════════════
def _stamp(frame: Any, label: str, cv2: Any) -> Any:
    h, w = frame.shape[:2]
    scale = max(0.5, w / 1100)
    thickness = max(1, int(round(scale * 2)))
    (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    cv2.rectangle(frame, (0, 0), (tw + 16, th + base + 12), (0, 0, 0), -1)
    cv2.putText(frame, label, (8, th + 6), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return frame


def _read_frame_at(cap: Any, t: float, fps: float, total_frames: int, cv2: Any) -> Any | None:
    idx = int(round(t * fps))
    if total_frames > 0:
        idx = min(idx, total_frames - 1)
    for back in (0, 3, 10):
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, idx - back))
        ok, frame = cap.read()
        if ok and frame is not None:
            return frame
    return None


def _open_capture(video: Path) -> Any:
    import cv2
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise MediaToolError(f"OpenCV không mở được video: {video}")
    return cap


def _extract_windows_to_dir(cap: Any, cv2: Any, windows: list[tuple[float, float]], out_dir: Path, cfg: Config,
                            fps: float, total_frames: int) -> list[FrameSample]:
    """Vòng lặp trích frame DÙNG CHUNG cho cả extract_samples() (toàn video) và
    extract_samples_in_range() (một macro-chunk). Ghi thẳng ra đĩa từng ảnh ngay khi có, KHÔNG giữ mảng
    pixel nào lại trong bộ nhớ Python sau khi ghi xong — bộ nhớ dùng cho bước này không phụ thuộc số
    lượng frame, chỉ phụ thuộc kích thước 1 frame tại một thời điểm."""
    out_dir.mkdir(parents=True, exist_ok=True)
    samples: list[FrameSample] = []
    failed = 0
    for ws, we in windows:
        for t in frame_times(ws, we, cfg):
            frame = _read_frame_at(cap, t, fps, total_frames, cv2)
            if frame is None:
                failed += 1
                log.warning("Không đọc được frame tại %s.", format_timestamp(t))
                continue
            h, w = frame.shape[:2]
            if w > cfg.frame_max_width:
                frame = cv2.resize(frame, (cfg.frame_max_width, max(1, int(h * cfg.frame_max_width / w))),
                                   interpolation=cv2.INTER_AREA)
            frame = _stamp(frame, format_timestamp(t), cv2)
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), cfg.jpeg_quality])
            if not ok:
                failed += 1
                continue
            idx = len(samples) + 1
            path = out_dir / f"frame_{idx:04d}_{int(t * 1000):08d}.jpg"
            path.write_bytes(buf.tobytes())            # Unicode-safe trên Windows; giải phóng `buf` ngay sau dòng này
            samples.append(FrameSample(idx, t, path, ws, we))
            del frame                                   # gợi ý GC giải phóng mảng pixel ngay, không đợi cuối vòng lặp lớn
    if not samples:
        raise MediaToolError("Không trích xuất được frame nào trong khoảng này (file hỏng hoặc codec không được hỗ trợ?).")
    log.info("Đã trích %d frame (%d lỗi) vào %s", len(samples), failed, out_dir)
    return samples


def extract_samples(cfg: Config, media: MediaInfo) -> list[FrameSample]:
    """Trích frame cho TOÀN BỘ video (dùng khi video ngắn hơn ngưỡng chia chunk — xem long_video_pipeline.py)."""
    try:
        import cv2
    except ImportError as e:
        raise PipelineError("Chưa cài OpenCV: pip install opencv-python") from e

    cuts = None
    if cfg.extract_mode == "scene":
        cuts = detect_scene_cuts(cfg.video_path, media, cfg.scene_threshold, cfg.min_scene_sec)
        log.info("OpenCV phát hiện %d cảnh cắt: %s", len(cuts), ", ".join(format_timestamp(c) for c in cuts[:12]) +
                 (" …" if len(cuts) > 12 else ""))
    windows = build_time_windows(media.duration_sec, cfg, cuts)
    log.info("Chia %d cửa sổ (chế độ '%s').", len(windows), cfg.extract_mode)

    cfg.frames_dir.mkdir(parents=True, exist_ok=True)
    for old in cfg.frames_dir.glob("frame_*.jpg"):
        old.unlink(missing_ok=True)
    cap = _open_capture(cfg.video_path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or media.fps
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return _extract_windows_to_dir(cap, cv2, windows, cfg.frames_dir, cfg, fps, total_frames)
    finally:
        cap.release()


def extract_samples_in_range(cfg: Config, media: MediaInfo, chunk_id: int, t0: float, t1: float,
                             scene_cuts_full_video: list[float] | None = None) -> list[FrameSample]:
    """Trích frame CHỈ trong khoảng [t0, t1) của MỘT macro-chunk, ghi vào `frames/chunk_XXXX/` riêng biệt.

    `scene_cuts_full_video`: nếu chunk_planner đã quét scene-cut cho TOÀN video một lần (để chọn điểm cắt
    chunk), truyền lại đây để KHÔNG quét lần thứ hai — chỉ lọc ra các mốc nằm trong [t0, t1).
    """
    try:
        import cv2
    except ImportError as e:
        raise PipelineError("Chưa cài OpenCV: pip install opencv-python") from e

    local_cuts = None
    if cfg.extract_mode == "scene":
        local_cuts = [c - t0 for c in (scene_cuts_full_video or []) if t0 < c < t1]
    windows = build_time_windows(t1 - t0, cfg, local_cuts)
    windows = [(ws + t0, we + t0) for ws, we in windows]         # đưa cửa sổ về mốc thời gian TUYỆT ĐỐI của video gốc

    out_dir = cfg.frames_dir / f"chunk_{chunk_id:04d}"
    if cfg.force:
        for old in out_dir.glob("frame_*.jpg"):
            old.unlink(missing_ok=True)
    cap = _open_capture(cfg.video_path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or media.fps
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return _extract_windows_to_dir(cap, cv2, windows, out_dir, cfg, fps, total_frames)
    finally:
        cap.release()          # giải phóng handle + bộ giải mã ngay khi xong CHUNK NÀY, không giữ tới cuối pipeline
