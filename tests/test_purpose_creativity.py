"""python tests/test_purpose_creativity.py — mục đích narration + mức độ sáng tạo: nối ĐÚNG vào cả 2 hàm
prompt (per_scene, continuous) chỉ qua _narrative_instructions() — không cần sửa gì ở build_batch_prompt/
build_continuous_batch_prompt."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import Config  # noqa: E402
from script_generator import (_PURPOSE_INSTRUCTIONS, build_batch_prompt,  # noqa: E402
                              build_continuous_batch_prompt)
from video_processor import FrameSample  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


batch = [FrameSample(1, 1.0, Path("/f1.jpg"), 0.0, 2.0)]

# ── 1) mọi mục đích đều sinh ra nội dung KHÁC NHAU, đúng chủ đề, và CÓ XUẤT HIỆN trong cả 2 loại prompt ──
for purpose in _PURPOSE_INSTRUCTIONS:
    cfg = Config(video_path=None, output_dir="/tmp/x", narration_purpose=purpose)
    p1 = build_batch_prompt(cfg, batch, 0.0, 2.0, 10.0, [])
    p2 = build_continuous_batch_prompt(cfg, batch, 0.0, 2.0, 10.0, [])
    expected = _PURPOSE_INSTRUCTIONS[purpose]
    if expected:
        check(f"mục đích '{purpose}': xuất hiện ĐÚNG trong prompt per_scene", expected in p1)
        check(f"mục đích '{purpose}': xuất hiện ĐÚNG trong prompt continuous", expected in p2)

check("8 mục đích đều cho nội dung KHÁC NHAU (không trùng lặp nhầm)",
      len({v for v in _PURPOSE_INSTRUCTIONS.values() if v}) == len([v for v in _PURPOSE_INSTRUCTIONS.values() if v]))

# ── 2) 3 mốc sáng tạo cho nội dung khác hẳn nhau, có trong cả 2 loại prompt ──
levels = {"thấp": 0.1, "vừa": 0.5, "cao": 0.9}
texts = {}
for label, lvl in levels.items():
    cfg = Config(video_path=None, output_dir="/tmp/x", creativity_level=lvl)
    p1 = build_batch_prompt(cfg, batch, 0.0, 2.0, 10.0, [])
    p2 = build_continuous_batch_prompt(cfg, batch, 0.0, 2.0, 10.0, [])
    check(f"mức sáng tạo {label} ({lvl}): có trong prompt per_scene lẫn continuous",
          "CREATIVITY LEVEL" in p1 and "CREATIVITY LEVEL" in p2)
    texts[label] = p1
check("3 mốc sáng tạo cho NỘI DUNG khác nhau rõ rệt (không phải cùng 1 câu lặp lại)",
      len({texts[k].split("CREATIVITY LEVEL")[1][:200] for k in texts}) == 3)
check("mức CAO vẫn có cảnh báo KHÔNG bịa tên riêng/sự kiện thật — an toàn dù sáng tạo cao",
      "invent specific names" in build_batch_prompt(
          Config(video_path=None, output_dir="/tmp/x", creativity_level=0.95), batch, 0.0, 2.0, 10.0, []))

# ── 3) giá trị biên đúng ranh giới 3 mốc (0.35 vẫn THẤP, 0.36 đã sang VỪA, 0.75 vẫn VỪA, 0.76 đã sang CAO) ──
from script_generator import _creativity_instruction
check("0.35 vẫn ở mức THẤP", "LOW" in _creativity_instruction(0.35))
check("0.36 đã chuyển sang VỪA", "MEDIUM" in _creativity_instruction(0.36))
check("0.75 vẫn ở mức VỪA", "MEDIUM" in _creativity_instruction(0.75))
check("0.76 đã chuyển sang CAO", "HIGH" in _creativity_instruction(0.76))
check("0.0 → THẤP (biên dưới)", "LOW" in _creativity_instruction(0.0))
check("1.0 → CAO (biên trên)", "HIGH" in _creativity_instruction(1.0))

# ── 4) validation Config ──
for bad_purpose in ("khong_ton_tai", "", "NEUTRAL"):
    try:
        Config(video_path=None, output_dir="/tmp/x", narration_purpose=bad_purpose)
        raise AssertionError(f"phải từ chối narration_purpose={bad_purpose!r}")
    except ValueError:
        pass
print("  ✓ narration_purpose không hợp lệ bị từ chối")
for bad_level in (-0.1, 1.1):
    try:
        Config(video_path=None, output_dir="/tmp/x", creativity_level=bad_level)
        raise AssertionError(f"phải từ chối creativity_level={bad_level}")
    except ValueError:
        pass
print("  ✓ creativity_level ngoài [0,1] bị từ chối")

# ── 5) mặc định (không set gì) vẫn hoạt động bình thường, không crash ──
cfg_default = Config(video_path=None, output_dir="/tmp/x")
p = build_batch_prompt(cfg_default, batch, 0.0, 2.0, 10.0, [])
check("mặc định: narration_purpose='neutral' → có chỉ dẫn trung lập", _PURPOSE_INSTRUCTIONS["neutral"] in p)
check("mặc định: creativity_level=0.3 → mức THẤP", "CREATIVITY LEVEL: LOW" in p)

print("TẤT CẢ PASS ✔")
