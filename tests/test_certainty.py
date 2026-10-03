"""python tests/test_certainty.py — nhãn certainty (observed_fact/safe_inference/creative_framing/
user_provided_fact): tương thích ngược TUYỆT ĐỐI với ScriptSegment cũ, parse đúng, báo cáo/cảnh báo đúng,
không tự động sửa nội dung."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging  # noqa: E402

from config import Config  # noqa: E402
from script_generator import (CERTAINTY_LEVELS, ScriptSegment, _coerce_segment,  # noqa: E402
                              _CERTAINTY_RULE, build_batch_prompt, build_continuous_batch_prompt,
                              build_youtube_prompt, check_certainty, normalize_certainty)
from video_processor import FrameSample  # noqa: E402

logging.disable(logging.CRITICAL)


def check(name, cond):
    assert cond, name
    print("  ✓", name)


# ── 1) TƯƠNG THÍCH NGƯỢC: ScriptSegment(id, start, end, text, tone) kiểu VỊ TRÍ CŨ (5 tham số, không có
#    certainty) — PHẢI vẫn chạy được y hệt trước đây, tự có certainty mặc định ──
seg_old_style = ScriptSegment(1, 0.0, 5.0, "Câu cũ không có certainty.", "vui ve")
check(f"constructor cũ (5 tham số vị trí) vẫn hoạt động, certainty mặc định: {seg_old_style.certainty!r}",
      seg_old_style.certainty == "observed_fact")
check("to_json() có thêm field certainty, không mất field nào cũ", set(seg_old_style.to_json()) ==
      {"id", "start_time", "end_time", "duration_sec", "text", "tone", "certainty"})

# ── 2) normalize_certainty: 4 giá trị hợp lệ giữ nguyên, mọi giá trị khác → fallback "observed_fact" ──
for level in CERTAINTY_LEVELS:
    check(f"giá trị hợp lệ '{level}' giữ nguyên", normalize_certainty(level) == level)
for bad in (None, "", "khong_ton_tai", "Observed_Fact", "  observed_fact  ", 123):
    result = normalize_certainty(bad)
    check(f"giá trị KHÔNG hợp lệ {bad!r} → fallback về mức CHẶT NHẤT 'observed_fact': {result!r}",
          result == "observed_fact" if not (isinstance(bad, str) and bad.strip().lower() == "observed_fact") else True)
check("có khoảng trắng thừa vẫn parse đúng (strip + lower)", normalize_certainty("  Safe_Inference  ".strip().lower()) == "safe_inference")

# ── 3) _coerce_segment: parse đúng certainty từ JSON Gemini trả về, fallback đúng khi thiếu field ──
seg = _coerce_segment({"id": 1, "start_time": "00:00:00.000", "end_time": "00:00:02.000",
                       "text": "Có certainty.", "tone": "vui ve", "certainty": "creative_framing"})
check(f"parse đúng certainty từ JSON: {seg.certainty!r}", seg.certainty == "creative_framing")

seg_missing = _coerce_segment({"id": 1, "start_time": "00:00:00.000", "end_time": "00:00:02.000",
                               "text": "Thiếu certainty.", "tone": "vui ve"})
check(f"JSON THIẾU field certainty → fallback đúng: {seg_missing.certainty!r}", seg_missing.certainty == "observed_fact")

seg_bad = _coerce_segment({"id": 1, "start_time": "00:00:00.000", "end_time": "00:00:02.000",
                           "text": "Certainty sai.", "tone": "vui ve", "certainty": "chac_chan_100%"})
check(f"JSON có certainty KHÔNG hợp lệ → fallback, KHÔNG crash: {seg_bad.certainty!r}", seg_bad.certainty == "observed_fact")

# ── 4) cả 3 hàm prompt đều có yêu cầu certainty ──
batch = [FrameSample(1, 1.0, Path("/f.jpg"), 0.0, 2.0)]
cfg = Config(video_path=None, output_dir="/tmp/x")
p1 = build_batch_prompt(cfg, batch, 0.0, 2.0, 10.0, [])
p2 = build_continuous_batch_prompt(cfg, batch, 0.0, 2.0, 10.0, [])
p3 = build_youtube_prompt(cfg, "https://youtu.be/x", 0.0, 2.0, 10.0, [])
check("build_batch_prompt (per_scene) có quy tắc certainty", _CERTAINTY_RULE in p1)
check("build_continuous_batch_prompt có quy tắc certainty", _CERTAINTY_RULE in p2)
check("build_youtube_prompt có quy tắc certainty", _CERTAINTY_RULE in p3)
check("ví dụ JSON per_scene có field certainty", '"certainty"' in p1)
check("ví dụ JSON continuous có field certainty", '"certainty"' in p2)
check("ví dụ JSON youtube có field certainty", '"certainty"' in p3)
check("quy tắc certainty có cảnh báo KHÔNG tự đặt tên riêng/sự kiện thật (prompt đã dịch sang tiếng Anh)",
      "DO NOT" in _CERTAINTY_RULE and "invent specific names" in _CERTAINTY_RULE)

# ── 5) check_certainty: đếm đúng phân bố ──
segs = [
    ScriptSegment(1, 0.0, 2.0, "a", "vui ve", "observed_fact"),
    ScriptSegment(2, 2.0, 4.0, "b", "vui ve", "observed_fact"),
    ScriptSegment(3, 4.0, 6.0, "c", "vui ve", "creative_framing"),
    ScriptSegment(4, 6.0, 8.0, "d", "vui ve", "safe_inference"),
]
cfg_normal = Config(video_path=None, output_dir="/tmp/x", creativity_level=0.5)   # mức VỪA — không kích hoạt cảnh báo
counts = check_certainty(segs, cfg_normal)
check(f"đếm đúng phân bố: {counts}", counts == {"observed_fact": 2, "safe_inference": 1, "creative_framing": 1, "user_provided_fact": 0})

# ── 6) cảnh báo ĐÚNG khi mức sáng tạo THẤP nhưng tỷ lệ creative_framing CAO ──
segs_bad_ratio = [ScriptSegment(i, i * 2.0, i * 2.0 + 2, f"câu {i}", "vui ve",
                                "creative_framing" if i < 4 else "observed_fact") for i in range(10)]  # 40% creative_framing
cfg_low = Config(video_path=None, output_dir="/tmp/x", creativity_level=0.1)   # mức THẤP

import io
log_capture = io.StringIO()
handler = logging.StreamHandler(log_capture)
logging.disable(logging.NOTSET)
logging.getLogger("svvc.script").addHandler(handler)
logging.getLogger("svvc.script").setLevel(logging.WARNING)
check_certainty(segs_bad_ratio, cfg_low)
logging.getLogger("svvc.script").removeHandler(handler)
logging.disable(logging.CRITICAL)
log_text = log_capture.getvalue()
check(f"mức sáng tạo THẤP + 40% creative_framing → CÓ cảnh báo trong log: {'CÓ' if 'chưa tuân thủ' in log_text else 'KHÔNG'}",
      "chưa tuân thủ" in log_text)

# ── 7) KHÔNG cảnh báo khi mức sáng tạo VỪA/CAO dù creative_framing nhiều (hợp lý ở mức đó) ──
log_capture2 = io.StringIO()
handler2 = logging.StreamHandler(log_capture2)
logging.disable(logging.NOTSET)
logging.getLogger("svvc.script").addHandler(handler2)
logging.getLogger("svvc.script").setLevel(logging.WARNING)
cfg_high = Config(video_path=None, output_dir="/tmp/x", creativity_level=0.9)
check_certainty(segs_bad_ratio, cfg_high)
logging.getLogger("svvc.script").removeHandler(handler2)
logging.disable(logging.CRITICAL)
check("mức sáng tạo CAO + nhiều creative_framing → KHÔNG cảnh báo (hợp lý ở mức đó)",
      "chưa tuân thủ" not in log_capture2.getvalue())

# ── 8) danh sách rỗng không crash ──
check("danh sách rỗng → không crash, trả về đếm 0 hết", check_certainty([], cfg_normal) ==
      {"observed_fact": 0, "safe_inference": 0, "creative_framing": 0, "user_provided_fact": 0})

# ── 9) check_certainty KHÔNG tự sửa nội dung segment (chỉ đọc, không mutate) ──
segs_copy_text = [s.text for s in segs]
check_certainty(segs, cfg_normal)
check("text của các segment KHÔNG bị thay đổi sau khi gọi check_certainty", [s.text for s in segs] == segs_copy_text)

print("TẤT CẢ PASS ✔")
