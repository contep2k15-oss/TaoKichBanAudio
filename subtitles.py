"""subtitles.py — Xuất phụ đề `.srt` từ kịch bản đã có, KHÔNG CẦN Whisper/forced-alignment.

Khác với kỹ thuật "nghe lại audio TTS để đo mốc thời gian thật" (forced alignment — một khâu RIÊNG, phức
tạp hơn nhiều, CHƯA làm trong dự án này), module này chỉ dùng THẲNG `start_sec`/`end_sec` đã có sẵn trong
từng `ScriptSegment` — đây CHÍNH LÀ mốc thời gian dùng để đặt audio lên video (`audio_assembler.py`), nên về
bản chất đã khá chính xác, không cần đo lại.

Có một điểm cần xử lý riêng: ở `narration_mode="continuous"`, MỖI SEGMENT là CẢ MỘT ĐOẠN VĂN dài (16-20s+)
— nếu xuất thẳng thành 1 dòng phụ đề duy nhất thì quá dài, không đọc kịp. Hàm `_split_for_subtitles()` chia
nhỏ đoạn văn đó thành từng câu (theo dấu kết câu), rồi PHÂN BỔ THỜI GIAN THEO TỶ LỆ SỐ KÝ TỰ của từng câu —
đây LÀ ước lượng gần đúng (ghi rõ trong code), không phải mốc thời gian thật đo được, vì không có forced
alignment để biết chính xác từng câu con bắt đầu/kết thúc lúc nào trong audio đã tổng hợp.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from script_generator import ScriptSegment

log = logging.getLogger("svvc.subtitles")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def _wrap_line(text: str, max_chars: int) -> str:
    """Xuống dòng ở khoảng trắng gần `max_chars` nhất, tối đa 2 dòng (quy ước phụ đề thông thường) — dòng
    thứ 3 trở đi (nếu còn) được nối thẳng vào cuối dòng 2, thà hơi dài còn hơn phụ đề quá nhiều dòng."""
    words = text.split()
    if not words:
        return text
    lines: list[str] = [""]
    for w in words:
        candidate = (lines[-1] + " " + w).strip()
        if len(candidate) <= max_chars or not lines[-1]:
            lines[-1] = candidate
        elif len(lines) < 2:
            lines.append(w)
        else:
            lines[-1] = (lines[-1] + " " + w).strip()
    return "\n".join(lines)


def _split_for_subtitles(text: str, start: float, end: float) -> list[tuple[float, float, str]]:
    """Chia 1 đoạn văn dài (continuous mode) thành nhiều câu con để hiện phụ đề — mốc thời gian từng câu
    con là ƯỚC LƯỢNG theo tỷ lệ số ký tự, KHÔNG PHẢI đo thật (xem docstring đầu file). Đoạn ngắn (per_scene
    mode, thường ≤ 1-2 câu) hầu như không bị chia, giữ nguyên gần như 1-1."""
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    if len(sentences) <= 1:
        return [(start, end, text.strip())]
    total_chars = sum(len(s) for s in sentences)
    if total_chars == 0:
        return [(start, end, text.strip())]
    out: list[tuple[float, float, str]] = []
    t = start
    for i, s in enumerate(sentences):
        remain = end - t
        dur = remain if i == len(sentences) - 1 else (end - start) * (len(s) / total_chars)
        out.append((t, min(t + dur, end), s))
        t += dur
    return out


def _srt_timestamp(sec: float) -> str:
    """Định dạng mốc thời gian theo ĐÚNG chuẩn SRT: `HH:MM:SS,mmm` — dùng DẤU PHẨY ngăn mili-giây, KHÁC với
    `utils.format_timestamp()` (dùng dấu chấm) vốn chỉ phục vụ nội bộ/log, không phải chuẩn phụ đề."""
    sec = max(0.0, sec)
    ms = round(sec * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def sentences_for_alignment(segments: list[ScriptSegment]) -> list[str]:
    """Tách MỌI segment (đã sắp theo `start_sec`) thành câu con, dùng đúng quy tắc tách câu như
    `_split_for_subtitles()` — danh sách này PHẢI dùng để đối chiếu với Whisper (`forced_alignment.py`), vì
    nó khớp ĐÚNG số lượng/thứ tự câu mà `build_srt()` sẽ tạo mục phụ đề, tránh lệch số câu giữa hai nơi."""
    out: list[str] = []
    for seg in sorted(segments, key=lambda s: s.start_sec):
        text = seg.text.strip()
        if not text:
            continue
        out.extend(s for s in _SENTENCE_SPLIT_RE.split(text) if s.strip())
    return out


def build_srt(segments: list[ScriptSegment], *, max_chars_per_line: int = 42,
             split_long_segments: bool = True, min_duration_sec: float = 0.5,
             aligned_cues: list[tuple[float, float]] | None = None) -> str:
    """Dựng nội dung file `.srt` từ danh sách `ScriptSegment` đã có (đã sắp xếp theo `start_sec` tăng dần).
    `split_long_segments=False`: giữ nguyên mỗi segment = đúng 1 mục phụ đề (không chia câu con) — dùng khi
    người gọi muốn phụ đề bám sát 1-1 với đúng cấu trúc kịch bản, kể cả ở continuous mode.
    `aligned_cues`: mốc thời gian THẬT đo bằng Whisper cho từng câu con (từ `forced_alignment.py`), ĐÚNG THỨ
    TỰ với `sentences_for_alignment(segments)` — khi có, DÙNG THAY cho ước lượng theo tỷ lệ ký tự nội bộ
    (`_split_for_subtitles`). Số lượng PHẢI khớp số câu thật tách được, nếu không sẽ bị bỏ qua (coi như
    không có) và quay về ước lượng cũ — tránh lắp sai mốc vào sai câu khi dữ liệu không khớp."""
    all_sentences = sentences_for_alignment(segments) if split_long_segments else None
    if aligned_cues is not None and all_sentences is not None and len(aligned_cues) != len(all_sentences):
        log.warning("Số mốc Whisper (%d) không khớp số câu thật (%d) — bỏ qua, dùng ước lượng theo ký tự.",
                   len(aligned_cues), len(all_sentences))
        aligned_cues = None

    cues: list[tuple[float, float, str]] = []
    if aligned_cues is not None and all_sentences is not None:
        for (start, end), text in zip(aligned_cues, all_sentences):
            if end - start < min_duration_sec:
                end = start + min_duration_sec
            cues.append((start, end, text))
    else:
        for seg in sorted(segments, key=lambda s: s.start_sec):
            text = seg.text.strip()
            if not text:
                continue
            pieces = _split_for_subtitles(text, seg.start_sec, seg.end_sec) if split_long_segments else \
                [(seg.start_sec, seg.end_sec, text)]
            for start, end, piece_text in pieces:
                if end - start < min_duration_sec:
                    end = start + min_duration_sec     # phụ đề quá ngắn rất khó đọc — nới nhẹ, không đè lên mục sau ở bước dưới
                cues.append((start, end, piece_text))

    # đảm bảo KHÔNG chồng lấn giữa 2 mục liên tiếp (có thể xảy ra sau bước "nới nhẹ" ở trên)
    for i in range(1, len(cues)):
        prev_start, prev_end, prev_text = cues[i - 1]
        start, end, text = cues[i]
        if start < prev_end:
            cues[i - 1] = (prev_start, start, prev_text)

    lines = []
    for i, (start, end, text) in enumerate(cues, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}")
        lines.append(_wrap_line(text, max_chars_per_line))
        lines.append("")
    return "\n".join(lines)


def write_srt(path: Path, segments: list[ScriptSegment], **kwargs) -> Path:
    path.write_text(build_srt(segments, **kwargs), encoding="utf-8")
    return path


def try_whisper_alignment(segments: list[ScriptSegment], voice_audio_path: Path, *, language: str = "vi",
                          model_size: str = "small") -> list[tuple[float, float]] | None:
    """Thử tinh chỉnh mốc phụ đề bằng Whisper — trả về danh sách mốc (ĐÚNG thứ tự với
    `sentences_for_alignment(segments)`) để truyền vào `build_srt(..., aligned_cues=...)`, hoặc `None` nếu
    không khả dụng/thất bại bất kỳ lý do gì (chưa cài `faster-whisper`, lỗi nhận dạng, số từ lệch quá xa...)
    — gọi nơi dùng CHỈ CẦN kiểm tra `is None` rồi tự rơi về `build_srt(segments)` bình thường, không cần biết
    lý do cụ thể. Đây là TOÀN BỘ phần CHƯA KIỂM CHỨNG được bằng Whisper thật trong sandbox phát triển (xem
    cảnh báo đầu `forced_alignment.py`) — nếu có lỗi khi chạy thật, nằm ở đây hoặc trong `forced_alignment.py`."""
    import forced_alignment
    if not forced_alignment.is_available():
        log.info("faster-whisper chưa được cài — dùng ước lượng theo ký tự (mặc định).")
        return None
    sentences = sentences_for_alignment(segments)
    if not sentences:
        return None
    words = forced_alignment.transcribe_words(voice_audio_path, language=language, model_size=model_size)
    if words is None:
        return None
    return forced_alignment.align_sentences_to_words(sentences, words)
