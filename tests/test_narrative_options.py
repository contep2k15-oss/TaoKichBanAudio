"""python tests/test_narrative_options.py — kiểm thử prompt sinh ra cho ngôi kể / phong cách kể chuyện."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import Config  # noqa: E402
from script_generator import _narrative_instructions, build_batch_prompt  # noqa: E402
from video_processor import FrameSample  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


sample = [FrameSample(1, 2.0, Path("f.jpg"), 0.0, 4.0)]

# 1) mặc định (natural, không có POV) → prompt KHÔNG có hướng dẫn ngôi kể/phong cách đặc biệt
cfg = Config(video_path=None, output_dir="/tmp/x1")
extra = _narrative_instructions(cfg, is_opening_batch=False, story_hook=None)
check("mặc định: không thêm hướng dẫn ngôi kể/phong cách nào", extra == "")

# 2) có narrator_pov → prompt PHẢI chứa đúng câu ngôi kể
cfg2 = Config(video_path=None, output_dir="/tmp/x2", narrator_pov="Chúng ta")
prompt2 = build_batch_prompt(cfg2, sample, 0.0, 4.0, 10.0, [])
check("prompt chứa đúng chỉ dẫn ngôi kể 'Chúng ta'", "Ngôi kể/xưng hô xuyên suốt: Chúng ta." in prompt2)

# 3) phong cách hài hước → prompt có từ khoá "hài hước"
cfg3 = Config(video_path=None, output_dir="/tmp/x3", narrative_style="humorous")
prompt3 = build_batch_prompt(cfg3, sample, 0.0, 4.0, 10.0, [])
check("phong cách 'humorous' → prompt có chỉ dẫn hài hước", "hài hước" in prompt3.lower())

# 4) fantasy_inspiring, LÔ MỞ ĐẦU (is_first_chunk=True, bi=1) → phải yêu cầu mở đầu bằng câu chuyện giả tưởng
cfg4 = Config(video_path=None, output_dir="/tmp/x4", narrative_style="fantasy_inspiring")
prompt4a = build_batch_prompt(cfg4, sample, 0.0, 4.0, 10.0, [], is_opening_batch=True, story_hook=None)
check("lô mở đầu: yêu cầu MỞ ĐẦU bằng câu chuyện giả tưởng", "CÂU CHUYỆN GIẢ TƯỞNG" in prompt4a)
check("lô mở đầu: vẫn nhắc phải bám sát hình ảnh cho phần sau", "không được bịa thêm nội dung" in prompt4a)

# 5) fantasy_inspiring, LÔ SAU (không phải mở đầu) với story_hook đã có → phải nhắc Gemini lồng ghép lại, KHÔNG yêu cầu mở đầu mới
prompt4b = build_batch_prompt(cfg4, sample, 20.0, 24.0, 10.0, [], is_opening_batch=False,
                              story_hook="Một chú robot nhỏ lạc giữa khu rừng công nghệ.")
check("lô sau: KHÔNG yêu cầu mở đầu mới bằng câu chuyện", "MỞ ĐẦU đoạn thuyết minh ĐẦU TIÊN" not in prompt4b)
check("lô sau: có nhắc lại đúng nội dung câu chuyện mở đầu đã lưu",
      "Một chú robot nhỏ lạc giữa khu rừng công nghệ." in prompt4b)
check("lô sau: yêu cầu chỉ lồng ghép THỈNH THOẢNG, không phải mọi đoạn", "THỈNH THOẢNG" in prompt4b)

# 6) fantasy_inspiring nhưng chưa có story_hook và KHÔNG phải lô mở đầu (vd sinh lại 1 lô lẻ) → không có hướng dẫn callback nào (an toàn)
prompt4c = build_batch_prompt(cfg4, sample, 20.0, 24.0, 10.0, [], is_opening_batch=False, story_hook=None)
check("chưa có story_hook, không phải lô mở đầu → không chèn hướng dẫn callback rác", "CÂU CHUYỆN GIẢ TƯỞNG" not in prompt4c
      and "THỈNH THOẢNG" not in prompt4c)

# 7) validation: narrative_style sai giá trị phải bị từ chối ngay khi tạo Config
try:
    Config(video_path=None, output_dir="/tmp/x5", narrative_style="khong_hop_le")
    raise AssertionError("phải ValueError")
except ValueError:
    print("  ✓ narrative_style sai giá trị → ValueError ngay khi tạo Config")

print("TẤT CẢ PASS ✔")
