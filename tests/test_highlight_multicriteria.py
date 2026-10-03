"""python tests/test_highlight_multicriteria.py — chấm điểm ĐA TIÊU CHÍ (story_value/visual_quality/
context_value/transition_value/redundancy_penalty) cho "Chỉ giữ cảnh hay": tổng hợp đúng trọng số, tương
thích ngược với cache định dạng điểm đơn giản cũ, không phá thuật toán select_highlights() lõi đã có."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from highlight_selector import (_CRITERIA_WEIGHTS, _REDUNDANCY_WEIGHT, _coerce_highlight,  # noqa: E402
                                _composite_score, build_highlight_prompt)


def check(name, cond):
    assert cond, name
    print("  ✓", name)


# ── 1) _composite_score: tổng hợp ĐÚNG công thức trọng số đã khai báo ──
raw = {"story_value": 1.0, "visual_quality": 1.0, "context_value": 1.0, "transition_value": 1.0, "redundancy_penalty": 0.0}
expected_max = sum(_CRITERIA_WEIGHTS.values()) * 10.0
check(f"mọi tiêu chí = 1.0, không trùng lặp → điểm TỐI ĐA đúng công thức: {_composite_score(raw)} == {expected_max}",
      abs(_composite_score(raw) - expected_max) < 1e-9)
check(f"tổng trọng số 4 tiêu chí chính CỐ Ý < 1.0 (dành dư địa, điểm tối đa {sum(_CRITERIA_WEIGHTS.values())*10:.1f}/10, "
      "tránh lạm phát điểm tuyệt đối)", 0.0 < sum(_CRITERIA_WEIGHTS.values()) <= 1.0)

raw_zero = {k: 0.0 for k in _CRITERIA_WEIGHTS}
raw_zero["redundancy_penalty"] = 0.0
check("mọi tiêu chí = 0 → điểm = 0", _composite_score(raw_zero) == 0.0)

# ── 2) redundancy_penalty TRỪ điểm, không cộng ──
raw_high = {"story_value": 0.8, "visual_quality": 0.8, "context_value": 0.8, "transition_value": 0.8, "redundancy_penalty": 0.0}
raw_redundant = dict(raw_high, redundancy_penalty=1.0)
s_high, s_redundant = _composite_score(raw_high), _composite_score(raw_redundant)
check(f"redundancy_penalty=1.0 làm GIẢM điểm so với redundancy=0: {s_redundant} < {s_high}", s_redundant < s_high)
check(f"mức giảm ĐÚNG bằng REDUNDANCY_WEIGHT × 10: giảm {s_high - s_redundant:.2f} ≈ {_REDUNDANCY_WEIGHT * 10:.2f}",
      abs((s_high - s_redundant) - _REDUNDANCY_WEIGHT * 10) < 1e-6)

# ── 3) điểm không bao giờ ÂM dù redundancy_penalty lớn hơn điểm tiêu chí ──
raw_tiny = {"story_value": 0.05, "visual_quality": 0.0, "context_value": 0.0, "transition_value": 0.0, "redundancy_penalty": 1.0}
check(f"điểm KHÔNG ÂM dù phạt nặng hơn điểm gốc: {_composite_score(raw_tiny)} >= 0", _composite_score(raw_tiny) >= 0.0)

# ── 4) giá trị NGOÀI [0,1] bị KẸP lại trước khi tính (Gemini có thể trả lệch) ──
raw_outlier = {"story_value": 5.0, "visual_quality": -2.0, "context_value": 0.5, "transition_value": 0.5, "redundancy_penalty": 3.0}
score_outlier = _composite_score(raw_outlier)
check(f"giá trị ngoài [0,1] bị kẹp, không cho điểm vô lý/âm: 0 <= {score_outlier} <= {expected_max}",
      0.0 <= score_outlier <= expected_max + 1e-6)

# ── 5) TƯƠNG THÍCH NGƯỢC: cache CŨ chỉ có field "score" đơn giản → dùng THẲNG, không áp công thức mới ──
raw_old = {"score": 7.5}
check(f"định dạng CŨ (chỉ có 'score') → dùng thẳng giá trị, KHÔNG tính composite: {_composite_score(raw_old)} == 7.5",
      _composite_score(raw_old) == 7.5)
raw_old_bad = {"score": "khong_phai_so"}
check(f"định dạng CŨ nhưng score hỏng → fallback an toàn (5.0), KHÔNG crash: {_composite_score(raw_old_bad)}",
      _composite_score(raw_old_bad) == 5.0)

# ── 6) thiếu field tiêu chí nào đó → coi như 0, KHÔNG crash ──
raw_partial = {"story_value": 0.8}
score_partial = _composite_score(raw_partial)
check(f"thiếu field (chỉ có story_value) → vẫn tính được, không crash: {score_partial}", score_partial >= 0.0)

# ── 7) _coerce_highlight: đường dẫn ĐẦY ĐỦ qua đa tiêu chí, kết quả hợp lý ──
h = _coerce_highlight({"start_time": "00:00:05.000", "end_time": "00:00:15.000", "story_value": 0.9,
                       "visual_quality": 0.8, "context_value": 0.5, "transition_value": 0.6,
                       "redundancy_penalty": 0.1}, t0=0.0, t1=20.0)
check(f"_coerce_highlight parse đúng đa tiêu chí, ra điểm hợp lý trong [1,10]: {h}", h is not None and 1.0 <= h[2] <= 10.0)

# ── 8) build_highlight_prompt: có đủ 5 tiêu chí trong hướng dẫn, có ví dụ JSON đúng schema ──
p = build_highlight_prompt(__import__("config").Config(video_path=None, output_dir="/tmp/x"),
                           [1.0, 3.0, 5.0], 0.0, 10.0, 60.0)
for crit in ("story_value", "visual_quality", "context_value", "transition_value", "redundancy_penalty"):
    check(f"prompt có hướng dẫn tiêu chí '{crit}'", crit in p)
check("prompt nhắc KHÔNG phải đoạn nào cũng chấm điểm cao (chấm trung thực)", "score HONESTLY" in p)
check("prompt cho phép trả mảng RỖNG nếu không có gì nổi bật", "EMPTY array" in p)

print("TẤT CẢ PASS ✔")
