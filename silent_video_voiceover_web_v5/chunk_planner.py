"""Phân đoạn động (Dynamic Chunking) cho video dài: chia video thành các MACRO-CHUNK vừa phải
(mặc định ~4 phút) để mỗi chunk được xử lý (trích frame → hỏi Gemini → TTS → ghép) VÀ checkpoint
ĐỘC LẬP với nhau — đây là điều kiện để (a) không tràn RAM (chỉ 1 chunk nằm trong bộ nhớ tại một thời
điểm) và (b) resume được giữa chừng (xem checkpoint.py, long_video_pipeline.py).

Thay vì cắt tại các mốc thời gian CỐ ĐỊNH (dễ cắt ngang câu nói / hành động đang diễn ra), điểm cắt được
chọn ưu tiên theo thứ tự:
  1. Giữa một khoảng LẶNG (từ silence_detector.py) gần mốc mục tiêu nhất — không ai đang nói ở đó.
  2. Một điểm CHUYỂN CẢNH (scene cut, từ video_processor.detect_scene_cuts) gần mốc mục tiêu — cắt cảnh
     ít nhất cũng không cắt ngang một hành động liên tục.
  3. Cắt CỨNG tại đúng mốc mục tiêu (kèm cảnh báo) — chỉ xảy ra khi video dài KHÔNG có khoảng lặng lẫn
     scene cut nào gần đó (vd giọng nói/nhạc liên tục không ngớt).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from utils import format_timestamp

log = logging.getLogger("svvc.chunker")


@dataclass(frozen=True)
class MacroChunk:
    index: int              # 0-based, dùng làm khoá checkpoint (chunk_0000, chunk_0001, ...)
    start_sec: float
    end_sec: float
    cut_reason: str          # "silence" | "scene" | "hard" | "final" (mép cuối video)

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec

    def __repr__(self) -> str:  # pragma: no cover
        return (f"MacroChunk(#{self.index} {format_timestamp(self.start_sec)}→{format_timestamp(self.end_sec)} "
                f"{self.duration_sec:.1f}s, cắt tại: {self.cut_reason})")


def _pick_cut(target: float, duration: float, tolerance: float, silences: list[tuple[float, float]],
             scene_cuts: list[float], min_start: float) -> tuple[float, str]:
    """Chọn điểm cắt tốt nhất gần `target`, không được sớm hơn `min_start` (đầu chunk hiện tại)."""
    best_t, best_reason, best_dist = target, "hard", tolerance + 1.0

    for s, e in silences:                                    # ưu tiên 1: điểm giữa khoảng lặng
        mid = (s + e) / 2
        if mid <= min_start or mid >= duration:
            continue
        dist = abs(mid - target)
        if dist <= tolerance and dist < best_dist:
            best_t, best_reason, best_dist = mid, "silence", dist

    if best_reason != "silence":                              # ưu tiên 2: điểm chuyển cảnh
        for c in scene_cuts:
            if c <= min_start or c >= duration:
                continue
            dist = abs(c - target)
            if dist <= tolerance and dist < best_dist:
                best_t, best_reason, best_dist = c, "scene", dist

    return best_t, best_reason


def plan_macro_chunks(duration_sec: float, *, target_sec: float = 240.0, tolerance_sec: float = 45.0,
                      min_chunk_sec: float = 60.0, silences: list[tuple[float, float]] | None = None,
                      scene_cuts: list[float] | None = None) -> list[MacroChunk]:
    """Chia [0, duration_sec] thành các MacroChunk liên tiếp, không chồng lấn, không có khoảng trống.

    Video ngắn hơn `target_sec` → trả về đúng 1 chunk bao trọn video (pipeline video ngắn = trường hợp
    đặc biệt N=1 của pipeline video dài, không cần code riêng — xem long_video_pipeline.py).
    """
    if duration_sec <= 0:
        raise ValueError("duration_sec phải > 0")
    if duration_sec <= target_sec:
        return [MacroChunk(0, 0.0, duration_sec, "final")]

    silences = sorted(silences or [])
    scene_cuts = sorted(scene_cuts or [])
    chunks: list[MacroChunk] = []
    start = 0.0
    idx = 0
    while start < duration_sec - 1e-6:
        remaining = duration_sec - start
        if remaining <= target_sec * 1.5:                    # đoạn cuối: gộp nốt, tránh chunk cuối quá ngắn
            chunks.append(MacroChunk(idx, start, duration_sec, "final"))
            break
        target = start + target_sec
        cut_t, reason = _pick_cut(target, duration_sec, tolerance_sec, silences, scene_cuts, start + min_chunk_sec)
        cut_t = max(start + min_chunk_sec, min(cut_t, duration_sec))
        if reason == "hard":
            log.warning("Chunk #%d: không tìm thấy khoảng lặng/scene-cut gần %s (±%.0fs) → cắt cứng tại đây "
                        "(có thể ngắt giữa câu nói).", idx, format_timestamp(target), tolerance_sec)
        else:
            log.info("Chunk #%d: cắt tại %s (%s, lệch %.1fs so với mốc mục tiêu %s).", idx,
                     format_timestamp(cut_t), "khoảng lặng" if reason == "silence" else "chuyển cảnh",
                     abs(cut_t - target), format_timestamp(target))
        chunks.append(MacroChunk(idx, start, cut_t, reason))
        start = cut_t
        idx += 1

    total = sum(c.duration_sec for c in chunks)
    assert abs(total - duration_sec) < 0.01, f"lỗi logic chia chunk: tổng {total} ≠ video {duration_sec}"
    log.info("Đã chia video %s thành %d chunk (mục tiêu %.0fs/chunk, dung sai ±%.0fs).",
             format_timestamp(duration_sec), len(chunks), target_sec, tolerance_sec)
    return chunks
