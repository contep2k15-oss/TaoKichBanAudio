"""python tests/test_story_plan.py — dàn ý tổng thể (enable_story_plan): mặc định TẮT không gọi Gemini thêm
lần nào (không đổi tốc độ cũ); khi bật, ĐÚNG 1 lượt gọi/CHUNK (không phải 1 lượt/lô), lỗi không chặn pipeline,
cache hoạt động, nội dung được nạp đúng vào mọi lô trong cùng chunk."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging  # noqa: E402

from config import Config  # noqa: E402
from script_generator import (_narrative_instructions, build_chunk_outline_prompt,  # noqa: E402
                              generate_script, plan_chunk_outline)
from video_processor import FrameSample  # noqa: E402

logging.disable(logging.CRITICAL)


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def clean_samples(n, window_len=2.0):
    return [FrameSample(i + 1, i * window_len + window_len / 2, Path(f"/f{i}.jpg"), i * window_len, (i + 1) * window_len)
           for i in range(n)]


def mk_cfg(tmp, **kw):
    v = tmp / "v.mp4"
    if not v.exists():
        v.write_bytes(b"x" * 100)
    return Config(video_path=v, output_dir=tmp / "out", words_per_sec=3.0, wps_tolerance=3.0, min_segment_sec=1.0, **kw)


class FakeMedia:
    duration_sec = 40.0


class CountingDriver:
    def __init__(self, outline_text="Mở đầu giới thiệu bối cảnh. Phát triển dần. Kết thúc nhẹ nhàng."):
        self.calls = []
        self.outline_text = outline_text

    def ask_json(self, prompt, images, *, label=""):
        self.calls.append((label, len(images), prompt))
        if "outline" in label:
            return [{"outline": self.outline_text}]
        return [{"id": 1, "start_time": "00:00:00.000", "end_time": "00:00:02.000", "text": "Một câu.", "tone": "vui ve"}]


# ── 1) _narrative_instructions: có dòng dàn ý khi truyền vào, KHÔNG có khi None ──
cfg0 = Config(video_path=None, output_dir="/tmp/x")
extra_with = _narrative_instructions(cfg0, is_opening_batch=False, story_hook=None, chunk_outline="Dàn ý test ABC.")
extra_without = _narrative_instructions(cfg0, is_opening_batch=False, story_hook=None, chunk_outline=None)
check("có chunk_outline → prompt chứa đúng nội dung dàn ý", "Dàn ý test ABC." in extra_with)
check("chunk_outline=None (mặc định) → KHÔNG có dòng dàn ý nào", "OVERALL OUTLINE" not in extra_without)

# ── 2) build_chunk_outline_prompt: nội dung đúng, yêu cầu JSON ──
p = build_chunk_outline_prompt(cfg0, [1.0, 5.0, 9.0], 0.0, 10.0, 60.0)
check("prompt yêu cầu dàn ý 3-5 câu", "3-5 sentences" in p)
check("prompt yêu cầu JSON có field outline", '"outline"' in p)
check("prompt nhắc KHÔNG viết lời thuyết minh thật ở bước này", "do NOT write actual narration" in p)

# ── 3) plan_chunk_outline: MẶC ĐỊNH TẮT (enable_story_plan=False) → KHÔNG gọi Gemini, trả về None ──
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg_off = mk_cfg(tmp, enable_story_plan=False)
    driver_off = CountingDriver()
    result = plan_chunk_outline(driver_off, clean_samples(5), cfg_off, FakeMedia(), "chunk0")
    check(f"TẮT (mặc định): trả về None, KHÔNG gọi Gemini lần nào: calls={driver_off.calls}",
          result is None and len(driver_off.calls) == 0)

# ── 4) BẬT → gọi Gemini ĐÚNG 1 lần, parse đúng outline, dùng mẫu THƯA (không phải mọi sample) ──
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg_on = mk_cfg(tmp, enable_story_plan=True)
    driver_on = CountingDriver("Đây là phần mở đầu của câu chuyện.")
    samples20 = clean_samples(20)
    result2 = plan_chunk_outline(driver_on, samples20, cfg_on, FakeMedia(), "chunk0")
    check(f"BẬT: gọi Gemini ĐÚNG 1 lần: {len(driver_on.calls)}", len(driver_on.calls) == 1)
    check(f"parse đúng nội dung outline: {result2!r}", result2 == "Đây là phần mở đầu của câu chuyện.")
    n_images_sent = driver_on.calls[0][1]
    check(f"dùng mẫu THƯA (ít hơn hẳn 20 sample gốc): {n_images_sent} < 20", n_images_sent < 20)

    # gọi lại lần 2 (cùng cfg, cùng samples) → phải dùng CACHE, không gọi Gemini thêm
    driver_on2 = CountingDriver()
    result3 = plan_chunk_outline(driver_on2, samples20, cfg_on, FakeMedia(), "chunk0")
    check("gọi lại (cache): KHÔNG gọi Gemini thêm lần nào", len(driver_on2.calls) == 0)
    check("kết quả từ cache giống hệt lần đầu", result3 == result2)

# ── 5) lỗi trong lúc lập dàn ý KHÔNG được chặn pipeline — trả về None, ghi log cảnh báo ──
class BrokenDriver:
    def ask_json(self, prompt, images, *, label=""):
        raise RuntimeError("Gemini lỗi giả lập")

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg_broken = mk_cfg(tmp, enable_story_plan=True)
    result4 = plan_chunk_outline(BrokenDriver(), clean_samples(5), cfg_broken, FakeMedia(), "chunk0")
    check("lỗi khi gọi Gemini → trả về None, KHÔNG ném exception (không chặn pipeline)", result4 is None)

# ── 6) tích hợp qua generate_script(): BẬT → ĐÚNG 1 lượt dàn ý/CHUNK, nạp đúng vào MỌI lô trong chunk đó ──
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg_int = mk_cfg(tmp, enable_story_plan=True, frames_per_prompt=4)
    driver_int = CountingDriver("Dàn ý chung cho cả chunk này.")
    samples_int = clean_samples(12)   # 12 ảnh, 4/lô → 3 lô trong CÙNG 1 chunk
    generate_script(driver_int, samples_int, cfg_int, FakeMedia(), batch_tag="chunkA")
    outline_calls = [c for c in driver_int.calls if "outline" in c[0]]
    batch_calls = [c for c in driver_int.calls if "outline" not in c[0]]
    check(f"ĐÚNG 1 lượt gọi dàn ý cho CẢ chunk (không phải 1 lượt/lô dù có 3 lô): {len(outline_calls)}", len(outline_calls) == 1)
    check(f"vẫn có 3 lượt gọi viết lời (1/lô) như bình thường: {len(batch_calls)}", len(batch_calls) == 3)
    check("dàn ý được NẠP ĐÚNG vào CẢ 3 prompt viết lời (không chỉ lô đầu)",
          all("Dàn ý chung cho cả chunk này." in c[2] for c in batch_calls))

# ── 7) MẶC ĐỊNH TẮT (enable_story_plan=False): generate_script() hoạt động Y HỆT TRƯỚC — không có dòng dàn ý ──
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg_default = mk_cfg(tmp)   # enable_story_plan=False mặc định
    driver_default = CountingDriver()
    generate_script(driver_default, clean_samples(8), cfg_default, FakeMedia(), batch_tag="chunkB")
    check("mặc định: KHÔNG có lượt gọi nào gắn nhãn 'outline'",
          not any("outline" in c[0] for c in driver_default.calls))
    check("mặc định: prompt viết lời KHÔNG có dòng dàn ý tổng thể",
          all("OVERALL OUTLINE" not in c[2] for c in driver_default.calls))

print("TẤT CẢ PASS ✔")
