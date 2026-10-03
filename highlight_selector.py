"""GIAI ĐOẠN 0 — Chỉ giữ cảnh hay (Highlight-only), chạy TRƯỚC `chunk_planner` để không lãng phí tài
nguyên xử lý TOÀN BỘ video khi người dùng chỉ muốn giữ lại những đoạn đắt giá nhất (`cfg.highlight_mode`).

Cách hoạt động (xem thêm giải thích kiến trúc trong `long_video_pipeline.py`):
  1. Lấy mẫu RẤT THƯA trên toàn bộ video gốc (`cfg.highlight_frame_interval_sec`, mặc định 6s — thưa hơn
     hẳn mức bình thường 2.5s dùng cho kịch bản chi tiết) — rẻ hơn nhiều lần so với phân tích đầy đủ.
  2. Gửi các ảnh này cho Gemini theo từng lô, hỏi "đâu là những đoạn đắt giá/đáng xem nhất trong khoảng
     này", kèm điểm số 1-10 cho mỗi đoạn được đề xuất.
  3. Gộp đề xuất từ MỌI lô (trải khắp video), xếp theo điểm số, chọn dần cho tới khi đạt tổng thời lượng
     mục tiêu (`cfg.highlight_target_ratio` × độ dài video gốc), rồi sắp lại theo thời gian để giữ mạch kể.
  4. Dùng FFmpeg CẮT từng đoạn được chọn (re-encode để đảm bảo cắt chính xác tại đúng khung hình, không
     lệch/hỏng ở điểm nối), rồi NỐI lại bằng FFmpeg concat demuxer (kỹ thuật đã dùng sẵn ở
     `video_muxer.concat_wavs`, an toàn với video dài, không dùng `-filter_complex` nhiều input) thành MỘT
     video MỚI, ngắn hơn hẳn bản gốc — toàn bộ pipeline phía sau (chunk, kịch bản, TTS, ghép) chạy trên
     video MỚI này, không đổi gì ở các bước đó.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from config import Config
from engines.base import BaseVisionEngine
from utils import (MediaInfo, MediaToolError, PipelineError, format_timestamp, parse_timestamp, read_json,
                   run_cmd, write_json)

log = logging.getLogger("svvc.highlight")


# ════════════════════════════════════════════════════════════
# 1) Chọn lọc: HÀM THUẦN, không I/O — dễ kiểm thử độc lập
# ════════════════════════════════════════════════════════════
def select_highlights(candidates: list[tuple[float, float, float]], target_total_sec: float,
                      *, min_gap_sec: float = 1e-3) -> list[tuple[float, float]]:
    """`candidates`: danh sách (start_sec, end_sec, score) Gemini đề xuất — CÓ THỂ chồng lấn nhau (nhiều lô
    liền kề có thể cùng đề xuất một đoạn tương tự). Chọn theo điểm số cao trước (ưu tiên "đắt giá nhất"),
    bỏ qua đoạn THỰC SỰ chồng lấn nội dung với đoạn ĐÃ chọn, dừng khi đạt `target_total_sec`. Kết quả trả
    về được SẮP LẠI THEO THỜI GIAN (giữ đúng mạch tự sự gốc của video, không xáo trộn theo điểm số).

    LƯU Ý: `min_gap_sec` chỉ để bù sai số làm tròn dấu phẩy động (mặc định gần như 0) — HAI ĐOẠN CHẠM SÁT
    NHAU (đoạn này kết thúc đúng lúc đoạn kia bắt đầu) KHÔNG bị coi là chồng lấn: đây là trường hợp BÌNH
    THƯỜNG khi một vùng "hay" trải dài qua ranh giới giữa hai lô quét liền kề — cả hai vẫn được giữ và khi
    cắt-ghép sẽ tự nhiên nối liền thành một đoạn cảnh liên tục, không đứt quãng."""
    if target_total_sec <= 0 or not candidates:
        return []
    ranked = sorted((c for c in candidates if c[1] > c[0]), key=lambda c: c[2], reverse=True)
    chosen: list[tuple[float, float]] = []
    total = 0.0
    for start, end, _score in ranked:
        if total >= target_total_sec:
            break
        if any(start < c_end - min_gap_sec and end > c_start + min_gap_sec for c_start, c_end in chosen):
            continue  # chồng lấn THỰC SỰ (không chỉ chạm biên) với đoạn đã chọn → bỏ qua, tránh trùng nội dung
        remaining = target_total_sec - total
        if end - start > remaining and chosen:
            end = start + remaining  # đoạn cuối cùng: cắt vừa đủ ngân sách còn lại (chỉ khi đã có ít nhất 1 đoạn)
        chosen.append((start, end))
        total += end - start
    chosen.sort(key=lambda w: w[0])
    return chosen


# ════════════════════════════════════════════════════════════
# 2) Prompt & bóc tách phản hồi Gemini
# ════════════════════════════════════════════════════════════
# Trọng số tổng hợp điểm ĐA TIÊU CHÍ thành 1 điểm cuối (thang 0-10) để đưa vào ĐÚNG `select_highlights()`
# hiện có — KHÔNG đổi thuật toán chọn lọc lõi đã kiểm chứng kỹ, chỉ làm GIÀU hơn cách Gemini tự chấm điểm.
_CRITERIA_WEIGHTS = {"story_value": 0.35, "visual_quality": 0.25, "context_value": 0.15, "transition_value": 0.15}
_REDUNDANCY_WEIGHT = 0.30   # phạt TRỪ, không cộng — đoạn càng lặp lại nội dung đã có càng bị trừ điểm


def build_highlight_prompt(cfg: Config, frame_timestamps: list[float], t0: float, t1: float,
                           video_duration: float) -> str:
    stamps = "\n".join(f"  Image {i}: {format_timestamp(t)}" for i, t in enumerate(frame_timestamps, start=1))
    example = [{"start_time": "00:01:12.000", "end_time": "00:01:20.000", "reason": "action climax, striking visual",
                "story_value": 0.9, "visual_quality": 0.8, "context_value": 0.5, "transition_value": 0.6,
                "redundancy_penalty": 0.1}]
    return (
        f"You are a professional highlight-reel editor. I'm attaching {len(frame_timestamps)} SPARSELY "
        f"sampled images from a video (total duration {format_timestamp(video_duration)}), only within the "
        f"range [{format_timestamp(t0)}, {format_timestamp(t1)}]. The top-left corner of each image has a "
        "timestamp label.\n"
        f"Order of images:\n{stamps}\n\n"
        "TASK: identify the most WATCHABLE/VALUABLE TIME RANGES in this segment (action climax, striking "
        "visuals, important moments, beautiful, funny, surprising...) — skip dull/repetitive/unremarkable "
        "stretches. Estimate the time boundaries based on the given images (you may interpolate between two "
        "adjacent images); they don't have to exactly match a specific image.\n\n"
        "Score EACH CRITERION separately (each on a 0.0–1.0 scale, score HONESTLY — not every segment should "
        "score high on every criterion):\n"
        "- story_value: how important this segment is to the OVERALL STORY ARC (an opening/climax/important "
        "turning point matters more than a pretty moment that doesn't affect the story).\n"
        "- visual_quality: pure visual quality (sharp, well-composed, well-lit, not shaky/blurry).\n"
        "- context_value: how necessary this is for the viewer to UNDERSTAND the context/what's happening (an "
        "explanatory/context-setting segment may be worth keeping even if not 'beautiful').\n"
        "- transition_value: whether this segment creates a SMOOTH transition into/out of the surrounding "
        "segments (an abrupt cut that's hard to connect scores low).\n"
        "- redundancy_penalty: how much this REPEATS content already shown elsewhere in the video (the more "
        "repetitive, the higher — a completely fresh segment gets 0).\n\n"
        "MANDATORY RULES:\n"
        "1. Return ONLY ONE ```json ... ``` code block containing a JSON array in this exact shape (no preamble):\n"
        f"```json\n{json.dumps(example, ensure_ascii=False, indent=2)}\n```\n"
        "2. start_time/end_time in HH:MM:SS.mmm format, MUST fall within the range given above; each segment "
        "2–15 seconds long.\n"
        "3. You may return an EMPTY array if this segment has nothing noteworthy — don't force in mediocre "
        "segments.\n"
        "4. Segments don't need to be contiguous, gaps between them are fine; they must not overlap each other."
    )


def _composite_score(raw: dict) -> float:
    """Tổng hợp điểm đa tiêu chí thành 1 con số (thang 0-10) để đưa vào `select_highlights()`. TƯƠNG THÍCH
    NGƯỢC: nếu `raw` chỉ có field `score` đơn giản (định dạng CŨ trước khi có đa tiêu chí — vd cache
    `highlight_batch_*.json` đã lưu từ trước, hoặc Gemini lỡ chỉ trả về đúng field này), dùng THẲNG giá trị
    đó, không tính composite — đảm bảo không phá vỡ cache cũ trên đĩa người dùng đang có sẵn."""
    if "score" in raw and not any(k in raw for k in _CRITERIA_WEIGHTS):
        try:
            return float(raw["score"])
        except (TypeError, ValueError):
            return 5.0
    weighted = sum(_CRITERIA_WEIGHTS[k] * max(0.0, min(1.0, float(raw.get(k, 0.0)))) for k in _CRITERIA_WEIGHTS)
    redundancy = max(0.0, min(1.0, float(raw.get("redundancy_penalty", 0.0))))
    return max(0.0, weighted - _REDUNDANCY_WEIGHT * redundancy) * 10.0


def _coerce_highlight(raw: Any, t0: float, t1: float) -> tuple[float, float, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        start = parse_timestamp(raw.get("start_time"))
        end = parse_timestamp(raw.get("end_time"))
        score = _composite_score(raw)
    except (ValueError, TypeError):
        return None
    if end <= start:
        return None
    start, end = max(start, t0), min(end, t1)
    if end <= start:
        return None
    return start, end, max(1.0, min(10.0, score))


# ════════════════════════════════════════════════════════════
# 3) Lấy mẫu thưa + gọi Gemini trên TOÀN VIDEO
# ════════════════════════════════════════════════════════════
def plan_highlight_windows(vision: BaseVisionEngine, cfg: Config, media: MediaInfo) -> list[tuple[float, float]]:
    """Trả về danh sách (start_sec, end_sec) đã chọn — sắp theo thời gian, phủ tổng cộng khoảng
    `cfg.highlight_target_ratio` × độ dài video. Danh sách RỖNG nghĩa là giữ nguyên video gốc (không cắt)."""
    import video_processor as vp

    duration = media.duration_sec
    n_windows = max(1, round(duration / cfg.highlight_frame_interval_sec))
    step = duration / n_windows
    frame_times = [(i + 0.5) * step for i in range(n_windows)]

    out_dir = cfg.workdir / "highlight_frames"
    # ĐÚNG 1 khung ở giữa mỗi cửa sổ (n_windows khung). Cách chính: MỘT lượt FFmpeg tuần tự (nhanh, dùng bộ
    # giải mã tốt nhất kể cả AV1); nếu lỗi mới rơi về OpenCV nhảy từng khung (chậm với video GOP dài).
    samples = None
    try:
        samples = vp.extract_uniform_frames_ffmpeg(cfg.video_path, start=step / 2, step=step, count=n_windows,
                                                   duration=duration, out_dir=out_dir, cfg=cfg)
    except Exception as e:  # noqa: BLE001 — mọi lỗi ở đường nhanh đều phải rơi về đường dự phòng, không được dừng cả pipeline
        log.warning("Quét nhanh bằng FFmpeg lỗi (%s) → dùng OpenCV.", e)
    if not samples:
        log.info("Dùng OpenCV để lấy khung (chậm hơn với video độ phân giải cao).")
        cap = vp._open_capture(cfg.video_path)  # noqa: SLF001 — tái dùng tiện ích nội bộ của video_processor
        try:
            import cv2
            fps = cap.get(cv2.CAP_PROP_FPS) or media.fps
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            windows = [(t - step / 2, t + step / 2) for t in frame_times]
            samples = vp._extract_windows_to_dir(cap, cv2, windows, out_dir, vp._OneFramePerWindowCfg(cfg), fps,  # noqa: SLF001
                                                 total_frames)
        finally:
            cap.release()

    per_prompt = min(cfg.frames_per_prompt, 10 if cfg.engine == "web" else 16)
    batches = [samples[i:i + per_prompt] for i in range(0, len(samples), per_prompt)]
    candidates: list[tuple[float, float, float]] = []
    log.info("Quét nhanh %d cửa sổ (%d lô) để tìm cảnh hay nhất...", len(samples), len(batches))
    for bi, batch in enumerate(batches, start=1):
        t0, t1 = batch[0].window_start, batch[-1].window_end
        log.info("Lô highlight %d/%d (%s → %s)...", bi, len(batches), format_timestamp(t0), format_timestamp(t1))
        prompt = build_highlight_prompt(cfg, [s.timestamp_sec for s in batch], t0, t1, duration)
        try:
            raw = vision.ask_json(prompt, [s.path for s in batch], label=f"highlight-{bi}")
        except PipelineError as e:
            log.warning("Lô highlight %d/%d lỗi (%s) → bỏ qua lô này, dùng các lô còn lại.", bi, len(batches), e)
            continue
        for item in raw:
            h = _coerce_highlight(item, t0, t1)
            if h:
                candidates.append(h)
        log.info("  → %d đoạn tiềm năng.", len([c for c in candidates if t0 <= c[0] < t1]))

    target = duration * cfg.highlight_target_ratio
    chosen = select_highlights(candidates, target)
    kept = sum(e - s for s, e in chosen)
    log.info("Đã chọn %d đoạn highlight, tổng %.0fs / %.0fs gốc (%.0f%%, mục tiêu %.0f%%).",
             len(chosen), kept, duration, 100 * kept / max(duration, 1e-6), cfg.highlight_target_ratio * 100)
    return chosen


# ════════════════════════════════════════════════════════════
# 4) Cắt + nối bằng FFmpeg (concat demuxer — an toàn với video dài, không filter_complex nhiều input)
# ════════════════════════════════════════════════════════════
def build_highlight_video(video: Path, windows: list[tuple[float, float]], out_path: Path, workdir: Path) -> None:
    if not windows:
        raise PipelineError("Không có đoạn highlight nào để dựng video.")
    scratch = workdir / "highlight_cuts"
    scratch.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for i, (start, end) in enumerate(windows):
            part = scratch / f"part_{i:04d}.mp4"
            # Re-encode (không dùng -c copy): cắt bằng stream-copy tại mốc KHÔNG PHẢI keyframe có thể lệch/lỗi
            # ở điểm nối — re-encode đảm bảo cắt CHÍNH XÁC đúng mốc và mọi phần đồng nhất định dạng để nối.
            run_cmd(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", video,
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", part],
                    f"cắt đoạn highlight [{start:.1f}s–{end:.1f}s]", timeout=1800)
            parts.append(part)

        list_file = scratch / "concat_list.txt"
        lines = ["file '" + str(p.resolve()).replace("'", "'\\''") + "'" for p in parts]
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_file,
                 "-c", "copy", out_path], f"nối {len(parts)} đoạn highlight (concat demuxer)", timeout=1800)
    finally:
        for p in parts:
            p.unlink(missing_ok=True)
        (scratch / "concat_list.txt").unlink(missing_ok=True)
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise MediaToolError("FFmpeg không tạo được video highlight.")


# ════════════════════════════════════════════════════════════
# 5) Điều phối cấp cao: mở Gemini riêng cho GIAI ĐOẠN 0 này, cache kết quả theo video gốc + tham số highlight
# ════════════════════════════════════════════════════════════
def _highlight_fingerprint(cfg: Config, original_video: Path, original_duration: float) -> dict[str, Any]:
    stat = original_video.stat()
    return {"video_name": original_video.name, "video_size": stat.st_size, "video_mtime": int(stat.st_mtime),
            "duration": round(original_duration, 2), "engine": cfg.engine,
            "gemini_model": cfg.gemini_model if cfg.engine == "api" else None,
            "highlight_target_ratio": cfg.highlight_target_ratio,
            "highlight_frame_interval_sec": cfg.highlight_frame_interval_sec}


def get_or_build_highlight_video(cfg: Config, media: MediaInfo, *, interactive: bool | None) -> Path:
    """Trả về đường dẫn video ĐÃ CẮT chỉ còn highlight — tự cache theo (video gốc + tham số highlight), nên
    chạy lại với cùng video/tham số sẽ dùng lại ngay, không tốn công quét/cắt lại. Nếu Gemini không tìm thấy
    đoạn nào đáng chú ý (hiếm, thường do video quá đồng đều), TỰ ĐỘNG dùng nguyên video gốc + cảnh báo, thay
    vì tạo ra một video rỗng/lỗi."""
    original_video = cfg.video_path
    fp = _highlight_fingerprint(cfg, original_video, media.duration_sec)
    fp_hash = hashlib.sha1(json.dumps(fp, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:12]
    hl_dir = cfg.workdir / "highlight"
    hl_dir.mkdir(parents=True, exist_ok=True)
    meta_path, video_out = hl_dir / f"{fp_hash}.json", hl_dir / f"{fp_hash}.mp4"

    if meta_path.is_file() and video_out.is_file():
        try:
            meta = read_json(meta_path)
            if meta.get("fingerprint") == fp:
                log.info("Đã có sẵn video highlight từ lần chạy trước (cùng video gốc + tham số) → dùng lại: %s "
                         "(%d đoạn, %.0fs).", video_out.name, len(meta.get("windows", [])), meta.get("kept_sec", 0))
                return video_out
        except PipelineError:
            pass
        log.info("Cấu hình/video gốc đã đổi khác lần trước → quét lại highlight từ đầu.")

    from engine_factory import open_vision_engine
    with open_vision_engine(cfg, interactive=interactive) as vision:
        windows = plan_highlight_windows(vision, cfg, media)

    if not windows:
        log.warning("Không tìm được đoạn nào đủ 'đắt giá' trong toàn video → GIỮ NGUYÊN video gốc, bỏ qua "
                    "chế độ highlight cho lần chạy này (thử hạ --highlight-target-ratio hoặc tắt highlight).")
        write_json(meta_path, {"fingerprint": fp, "windows": [], "kept_sec": media.duration_sec, "fallback": True})
        return original_video

    build_highlight_video(original_video, windows, video_out, cfg.workdir)
    kept_sec = sum(e - s for s, e in windows)
    write_json(meta_path, {"fingerprint": fp, "windows": windows, "kept_sec": kept_sec})
    log.info("Video highlight: %.0fs → %.0fs (%d đoạn, còn %.0f%% so với gốc) → %s", media.duration_sec, kept_sec,
             len(windows), 100 * kept_sec / max(media.duration_sec, 1e-6), video_out.name)
    return video_out
