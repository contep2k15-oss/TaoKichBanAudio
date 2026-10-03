"""python tests/test_forced_alignment.py — align_sentences_to_words(): thuật toán đối chiếu THUẦN (không
gọi Whisper thật), test bằng dữ liệu giả lập mô phỏng đúng định dạng Whisper trả về."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forced_alignment import WordTiming, align_sentences_to_words, is_available  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def words_from(text_with_timing: list[tuple[str, float, float]]) -> list[WordTiming]:
    return [WordTiming(w, s, e) for w, s, e in text_with_timing]


# ── 1) KHỚP HOÀN HẢO: Whisper nhận đúng y hệt số từ ──
sentences = ["Xin chào các bạn", "Hôm nay trời đẹp"]
# "Xin chào các bạn" = 4 từ, "Hôm nay trời đẹp" = 4 từ → tổng 8 từ
words = words_from([
    ("Xin", 0.0, 0.2), ("chào", 0.2, 0.5), ("các", 0.5, 0.7), ("bạn", 0.7, 1.0),
    ("Hôm", 1.2, 1.4), ("nay", 1.4, 1.6), ("trời", 1.6, 1.9), ("đẹp", 1.9, 2.2),
])
result = align_sentences_to_words(sentences, words)
check(f"khớp hoàn hảo → ĐÚNG 2 câu: {result}", result is not None and len(result) == 2)
check(f"câu 1 mốc đúng [từ đầu.start, từ cuối.end] = [0.0, 1.0]: {result[0]}", result[0] == (0.0, 1.0))
check(f"câu 2 mốc đúng [1.2, 2.2]: {result[1]}", result[1] == (1.2, 2.2))

# ── 2) LỆCH NHẸ (trong ngưỡng cho phép, mặc định 25%) — vẫn đối chiếu được, không trả None ──
sentences2 = ["Một hai ba bốn năm sáu bảy tám"]  # 8 từ kỳ vọng
words2 = words_from([(f"w{i}", i * 0.2, i * 0.2 + 0.15) for i in range(7)])  # Whisper chỉ nhận 7 từ (lệch 12.5%)
result2 = align_sentences_to_words(sentences2, words2)
check(f"lệch 12.5% (trong ngưỡng 25%) → vẫn đối chiếu được, KHÔNG trả None: {result2}", result2 is not None)

# ── 3) LỆCH QUÁ XA (vượt ngưỡng) → từ chối hẳn, trả None (KHÔNG cố ước lượng mù) ──
sentences3 = ["Một hai ba bốn năm sáu bảy tám chín mười"]  # 10 từ kỳ vọng
words3 = words_from([(f"w{i}", i * 0.1, i * 0.1 + 0.08) for i in range(3)])  # Whisper chỉ nhận 3 từ (lệch 70%)
result3 = align_sentences_to_words(sentences3, words3)
check(f"lệch 70% (vượt xa ngưỡng 25%) → từ chối, trả None (an toàn hơn ước lượng mù): {result3}", result3 is None)

# ── 4) có thể TUỲ CHỈNH ngưỡng cho phép ──
result3b = align_sentences_to_words(sentences3, words3, max_count_mismatch_ratio=0.8)
check(f"nới ngưỡng lên 80% → giờ CHẤP NHẬN được mức lệch 70% trước đó: {result3b}", result3b is not None)

# ── 5) Whisper hết từ SỚM HƠN dự kiến (trong ngưỡng cho phép) — câu cuối dùng mốc từ cuối cùng đã thấy,
#    KHÔNG trả về khoảng thời gian âm/vô nghĩa ──
sentences4 = ["Một hai ba", "Bốn năm sáu", "Bảy tám chín"]  # 3+3+3 = 9 từ kỳ vọng
words4 = words_from([(f"w{i}", i * 0.3, i * 0.3 + 0.25) for i in range(8)])  # Whisper chỉ nhận 8 từ (lệch ~11%)
result4 = align_sentences_to_words(sentences4, words4)
check(f"3 câu, Whisper thiếu 1 từ cuối cùng → vẫn ra đủ 3 câu, không crash: {result4}",
      result4 is not None and len(result4) == 3)
check("mọi mốc thời gian đều HỢP LỆ (end >= start), không có khoảng âm", all(e >= s for s, e in result4))

# ── 6) danh sách câu RỖNG → trả về [] (khác None — phân biệt rõ 'không có gì để làm' với 'làm thất bại') ──
check("câu rỗng → [] (không phải None)", align_sentences_to_words([], words) == [])

# ── 7) Whisper không nhận được từ nào (audio câm/lỗi) → None ──
check("Whisper không có từ nào → None", align_sentences_to_words(sentences, []) is None)

# ── 8) is_available(): không crash dù chưa/đã cài faster-whisper ──
avail = is_available()
check(f"is_available() chạy được, trả về bool: {avail}", isinstance(avail, bool))

print("TẤT CẢ PASS ✔")
