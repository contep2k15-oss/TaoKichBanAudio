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
import time
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
    total_expected = sum(len(frame_times(ws, we, cfg)) for ws, we in windows)
    t_start = time.perf_counter()
    done = 0
    for ws, we in windows:
        for t in frame_times(ws, we, cfg):
            done += 1
            # Hiện tiến độ + thời gian còn lại ước tính: bước này có thể rất chậm với video độ phân giải cao /
            # codec khó giải mã (AV1...) — nếu im lặng hàng chục phút, người dùng không phân biệt được "đang
            # chạy" với "đã treo". Ghi log mỗi 8 khung hình (INFO, hiện cả trên GUI/log file).
            if done == 1 or done % 8 == 0:
                elapsed = time.perf_counter() - t_start
                per = elapsed / max(done - 1, 1) if done > 1 else 0.0
                eta = per * (total_expected - done + 1)
                log.info("Trích frame %d/%d%s", done, total_expected,
                         f" — {per:.1f}s/khung, còn ~{eta / 60:.1f} phút" if done > 1 else "")
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


def extract_uniform_frames_ffmpeg(video: Path, *, start: float, step: float, count: int, duration: float,
                                  out_dir: Path, cfg: Config) -> list[FrameSample] | None:
    """Lấy `count` khung CÁCH ĐỀU `step` giây (khung đầu tại `start`) bằng MỘT lượt FFmpeg tuần tự.

    Vì sao không dùng OpenCV nhảy tới từng khung (`_read_frame_at`): mỗi lần nhảy phải giải mã lại từ khung
    khoá (keyframe) gần nhất tới khung đích — với video AV1/VP9 GOP dài (kiểu tải từ YouTube) mỗi khung có
    thể mất hàng giây (đo thực tế trên máy người dùng: ~4,7 giây/khung ở 1080p → 380 khung ≈ 30 phút). Ngoài
    ra bộ giải mã trong OpenCV không đảm bảo hỗ trợ AV1 (đo: 0/12 khung ở môi trường thử). FFmpeg đọc tuần tự
    một lần, dùng bộ giải mã tốt nhất (dav1d cho AV1, đa luồng) và dừng ngay khi đủ `count` khung.

    Trả về None nếu FFmpeg lỗi/không ra khung nào — người gọi tự rơi về đường OpenCV. Mỗi ảnh được đóng dấu
    thời gian (giống đường OpenCV) và đặt tên theo cùng quy ước `frame_{idx}_{ms}.jpg`."""
    import subprocess

    import numpy as np

    try:
        import cv2
    except ImportError:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("ff_*.jpg")) + list(out_dir.glob("frame_*.jpg")):
        old.unlink(missing_ok=True)

    q = max(2, min(31, round((100 - cfg.jpeg_quality) * 0.3) + 2))          # thang chất lượng mjpeg: 2 (tốt nhất)…31
    # QUAN TRỌNG: filter `fps` căn khung theo LƯỚI THỜI GIAN TUYỆT ĐỐI (bội số của `step`), KHÔNG theo điểm bắt
    # đầu — dùng thẳng `fps=1/step` với start=3, step=6 sẽ lấy khung ở 6, 12, 18… thay vì 3, 9, 15… (lệch tới
    # nửa cửa sổ; đã đo thực tế). Nên dịch trục thời gian đi `delta = start mod step` để các mốc mong muốn
    # trùng đúng lưới, rồi mới lấy mẫu.
    delta = start % step
    vf = (f"setpts=PTS-{delta:.6f}/TB,fps=1/{step:.6f},"
          f"scale=w='min({cfg.frame_max_width},iw)':h=-2")
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
           "-ss", f"{start:.3f}", "-i", str(video), "-an", "-sn", "-dn", "-vf", vf,
           "-frames:v", str(count), "-q:v", str(q), "-start_number", "0", str(out_dir / "ff_%05d.jpg")]
    log.info("Quét nhanh bằng FFmpeg (một lượt tuần tự): %d khung, cách nhau %.1fs...", count, step)
    t_begin = time.perf_counter()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log.warning("Không tìm thấy ffmpeg trong PATH → dùng OpenCV.")
        return None

    total_out = max(count * step, 1.0)
    last_log, err_tail = t_begin, []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                done_sec = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            now = time.perf_counter()
            if now - last_log >= 15:                     # báo tiến độ mỗi ~15 giây
                last_log = now
                frac = min(max(done_sec / total_out, 0.0), 1.0)
                eta = (now - t_begin) * (1 - frac) / frac if frac > 0.01 else 0
                log.info("Quét nhanh: %.0f%%%s", frac * 100, f" — còn ~{eta / 60:.1f} phút" if eta else "")
        elif "=" not in line and line:
            err_tail.append(line)
    rc = proc.wait()
    files = sorted(out_dir.glob("ff_*.jpg"))
    if rc != 0 or not files:
        log.warning("FFmpeg quét nhanh thất bại (mã %s, %d ảnh): %s", rc, len(files), " | ".join(err_tail[-3:]))
        return None

    samples: list[FrameSample] = []
    for k, f in enumerate(files):
        t = start + k * step
        img = cv2.imdecode(np.frombuffer(f.read_bytes(), np.uint8), cv2.IMREAD_COLOR)   # Unicode-safe trên Windows
        f.unlink(missing_ok=True)
        if img is None:
            continue
        img = _stamp(img, format_timestamp(t), cv2)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), cfg.jpeg_quality])
        if not ok:
            continue
        idx = len(samples) + 1
        path = out_dir / f"frame_{idx:04d}_{int(t * 1000):08d}.jpg"
        path.write_bytes(buf.tobytes())
        samples.append(FrameSample(idx, t, path, max(0.0, t - step / 2), min(duration, t + step / 2)))
    log.info("Quét nhanh xong: %d khung trong %.0fs (%.2fs/khung).", len(samples), time.perf_counter() - t_begin,
             (time.perf_counter() - t_begin) / max(len(samples), 1))
    return samples or None


class _OneFramePerWindowCfg:
    """Cấu hình tối thiểu ép `frame_times()` trả ĐÚNG 1 khung/cửa sổ (dùng cho đường dự phòng OpenCV của bước
    quét highlight — trước đây dùng chung khoảng lấy mẫu 2,5s nên ra 2 khung/cửa sổ 6s, gấp đôi cần thiết)."""

    def __init__(self, cfg: Config):
        self.max_frames_per_window = 1
        self.frame_interval_sec = cfg.frame_interval_sec
        self.frame_max_width = cfg.frame_max_width
        self.jpeg_quality = cfg.jpeg_quality


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
