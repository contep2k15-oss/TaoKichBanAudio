"""forced_alignment.py — Dùng Whisper nghe lại audio TTS ĐÃ TỔNG HỢP (không phải video gốc) để tìm mốc thời
gian CHÍNH XÁC của từng câu con bên trong một đoạn văn dài (`narration_mode="continuous"`), thay cho cách
ước lượng theo tỷ lệ số ký tự hiện có trong `subtitles.py` — chỉ ảnh hưởng tới ĐỘ CHÍNH XÁC của phụ đề
`.srt`, KHÔNG ảnh hưởng gì tới việc đặt audio lên video (vẫn dùng mốc `start_sec`/`end_sec` + atempo như cũ).

═══════════════════════════════════════════════════════════════════════════════════════════════════════
LƯU Ý QUAN TRỌNG VỀ MỨC ĐỘ KIỂM CHỨNG CỦA FILE NÀY (đọc trước khi dùng thật):

Sandbox dùng để phát triển dự án này CHẶN truy cập huggingface.co (nơi tải model Whisper) — nghĩa là hàm
`transcribe_words()` (gọi `faster-whisper` thật) CHƯA TỪNG được chạy thử với model/audio thật trong môi
trường phát triển. Đã viết đúng theo tài liệu API chính thức của `faster-whisper`, nhưng CHƯA tự xác nhận
bằng kết quả thật như mọi phần khác của dự án này.

Ngược lại, `align_sentences_to_words()` — phần thuật toán ĐỐI CHIẾU câu đã biết với danh sách từ Whisper trả
về — là HÀM THUẦN (không gọi Whisper, không I/O), đã được test đầy đủ bằng dữ liệu giả lập mô phỏng đúng
định dạng Whisper trả về (xem `tests/test_forced_alignment.py`).

Nếu `transcribe_words()` gặp lỗi bất ngờ khi chạy thật (sai tham số API, phiên bản thư viện khác...), toàn
bộ tính năng tự động QUAY VỀ cách ước lượng cũ (xem `subtitles.py`) — không làm hỏng việc xuất phụ đề, chỉ
mất đi phần cải thiện độ chính xác.
═══════════════════════════════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("svvc.align")


@dataclass
class WordTiming:
    word: str
    start: float
    end: float


def is_available() -> bool:
    """True nếu `faster-whisper` đã được cài — dùng để GUI/CLI biết có nên hiện tuỳ chọn này hay không,
    và để `transcribe_words()` tự biết khi nào nên bỏ cuộc sớm thay vì cố import rồi crash."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


# ════════════════════════════════════════════════════════════
# Phần CHƯA KIỂM CHỨNG THẬT (xem cảnh báo đầu file) — gọi faster-whisper thật
# ════════════════════════════════════════════════════════════
def transcribe_words(audio_path: Path, language: str = "vi", model_size: str = "small") -> list[WordTiming] | None:
    """Trả về danh sách từ + mốc thời gian (giây, tính từ đầu `audio_path`) do Whisper nhận dạng được, hoặc
    `None` nếu `faster-whisper` chưa cài / lỗi bất kỳ lúc nào (KHÔNG ném exception ra ngoài — đây là tính
    năng LÀM GIÀU THÊM độ chính xác phụ đề, lỗi ở đây không được phép làm hỏng cả việc xuất video/phụ đề).

    `model_size`: "tiny"/"base"/"small"/"medium"/"large-v3" — model càng lớn càng chính xác nhưng càng chậm
    và tốn bộ nhớ hơn; "small" là điểm cân bằng hợp lý cho việc NGHE LẠI CHÍNH GIỌNG MÁY TTS (rõ ràng, không
    ồn, không giống độ khó của audio đời thực) — không cần model lớn như khi nhận dạng giọng người thật."""
    if not is_available():
        log.warning("faster-whisper chưa được cài — bỏ qua bước căn chỉnh chính xác, dùng ước lượng cũ.")
        return None
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(audio_path), language=language, word_timestamps=True)
        words: list[WordTiming] = []
        for seg in segments:
            for w in (seg.words or []):
                words.append(WordTiming(w.word.strip(), w.start, w.end))
        if not words:
            log.warning("Whisper không nhận dạng được từ nào trong %s — dùng ước lượng cũ.", audio_path)
            return None
        return words
    except Exception as e:  # noqa: BLE001 — xem docstring: lỗi ở đây KHÔNG được chặn pipeline
        log.warning("Whisper gặp lỗi khi nhận dạng lại audio (%s) — dùng ước lượng cũ thay thế.", e)
        return None


# ════════════════════════════════════════════════════════════
# Phần ĐÃ KIỂM CHỨNG ĐẦY ĐỦ — thuật toán thuần, không I/O (xem tests/test_forced_alignment.py)
# ════════════════════════════════════════════════════════════
def align_sentences_to_words(sentences: list[str], words: list[WordTiming],
                             *, max_count_mismatch_ratio: float = 0.25) -> list[tuple[float, float]] | None:
    """Đối chiếu danh sách CÂU ĐÃ BIẾT TRƯỚC (chính là văn bản gửi cho TTS, biết chính xác từng chữ) với
    danh sách TỪ Whisper nhận dạng lại được từ audio đã tổng hợp — vì đây là audio MÁY TỰ ĐỌC (không phải
    audio người thật ồn/nhoè), số từ Whisper nhận ra thường khớp khá sát số từ gốc, nên có thể dùng cách đơn
    giản: câu thứ i có N từ → lấy ĐÚNG N từ tiếp theo trong danh sách Whisper, mốc câu = [từ_đầu.start,
    từ_cuối.end]. Không cần so khớp CHỮ (vì Whisper có thể viết hoa/thường hoặc tách dấu câu khác bản gốc),
    chỉ cần ĐẾM đúng số lượng.

    Trả về `None` (KHÔNG cố gắng đối chiếu từng phần) nếu tổng số từ Whisper lệch QUÁ XA tổng số từ kỳ vọng
    (`max_count_mismatch_ratio`, mặc định cho phép lệch tới 25%) — lệch nhiều là dấu hiệu Whisper nhận dạng
    sai đáng kể (có thể bỏ sót cả câu, hoặc audio có vấn đề), đối chiếu mù theo số lượng trong trường hợp đó
    sẽ cho ra mốc thời gian SAI HẲN cho mọi câu sau điểm lệch — thà quay về ước lượng cũ còn hơn.

    LƯU Ý: không có cách nào phân biệt "văn bản không có câu nào" khỏi "input rỗng" — cả hai đều trả `[]`
    (không phải `None`) để phân biệt rõ với trường hợp "có input nhưng đối chiếu thất bại hẳn"."""
    if not sentences:
        return []
    expected_word_counts = [max(1, len(s.split())) for s in sentences]
    expected_total = sum(expected_word_counts)
    actual_total = len(words)
    if expected_total == 0 or actual_total == 0:
        return None
    mismatch_ratio = abs(actual_total - expected_total) / expected_total
    if mismatch_ratio > max_count_mismatch_ratio:
        log.warning("Số từ Whisper nhận dạng (%d) lệch quá xa số từ kỳ vọng (%d, lệch %.0f%%) — bỏ qua đối "
                   "chiếu, dùng ước lượng cũ.", actual_total, expected_total, mismatch_ratio * 100)
        return None

    out: list[tuple[float, float]] = []
    idx = 0
    for n in expected_word_counts:
        chunk = words[idx:idx + n]
        if not chunk:
            # hết từ Whisper sớm hơn dự kiến (lệch nhưng vẫn trong ngưỡng cho phép) — câu còn lại dùng
            # NGUYÊN mốc của từ CUỐI CÙNG đã thấy, để KHÔNG trả về khoảng thời gian âm/vô nghĩa
            last = out[-1][1] if out else 0.0
            out.append((last, last))
            continue
        out.append((chunk[0].start, chunk[-1].end))
        idx += n
    return out
