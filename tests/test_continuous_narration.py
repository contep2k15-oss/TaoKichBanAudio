"""python tests/test_continuous_narration.py — chế độ "Đọc liên tục" (narration_mode='continuous'): mỗi lô
ảnh phải sinh ĐÚNG 1 ScriptSegment dài (không chia nhỏ từng câu), prompt yêu cầu đúng, và toàn bộ hạ tầng
phía sau (validate_and_fix, cache, story_hook) vẫn hoạt động KHÔNG ĐỔI."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging  # noqa: E402

from config import Config  # noqa: E402
from script_generator import ScriptSegment, build_continuous_batch_prompt, generate_script, max_words_for  # noqa: E402
from video_processor import FrameSample  # noqa: E402

logging.disable(logging.CRITICAL)


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def fake_samples(n, start=0.0, step=2.0):
    return [FrameSample(i + 1, start + i * step, Path(f"/fake/f{i}.jpg"), start + i * step - step / 2, start + i * step + step / 2)
           for i in range(n)]


def clean_samples(n, window_len, window_step):
    """Dựng FrameSample với ranh giới cửa sổ SẠCH, KHÔNG âm (tránh vướng quirk có sẵn, không liên quan tới
    tính năng đang test: format_timestamp() vốn đã clamp số âm về 0 — hành vi cũ, không phải lỗi mới)."""
    out = []
    for i in range(n):
        ws = i * window_step
        we = ws + window_len
        out.append(FrameSample(i + 1, (ws + we) / 2, Path(f"/fake/f{i}.jpg"), ws, we))
    return out


def batch_window(samples, per_prompt, batch_idx):
    """Ranh giới (t0, t1) THẬT mà generate_script() sẽ dùng cho đúng lô thứ `batch_idx` (0-based) — tính
    y hệt cách video_processor.plan_batches chia lô, để test so sánh đúng giá trị thật, không đoán mò."""
    batch = samples[batch_idx * per_prompt:(batch_idx + 1) * per_prompt]
    return batch[0].window_start, batch[-1].window_end


# ── 1) build_continuous_batch_prompt: đúng nội dung, đúng ràng buộc ──
cfg = Config(video_path=None, output_dir="/tmp/x", narration_mode="continuous", words_per_sec=2.0)
batch = fake_samples(8, start=0.0, step=2.5)
p = build_continuous_batch_prompt(cfg, batch, 0.0, 20.0, 100.0, [])
check("yêu cầu MỘT đoạn văn liền mạch (không chia câu rời) — prompt đã dịch sang tiếng Anh",
      "A SINGLE CONTINUOUS FLOWING PARAGRAPH" in p)
check("yêu cầu giữ dấu câu tự nhiên", "Keep FULL, natural punctuation" in p)
check("yêu cầu KHÔNG viết câu cụt rời rạc", "disconnected" in p and "list-like sentences" in p)
check("mốc thời gian t0/t1 CỐ ĐỊNH, không để Gemini tự chọn",
      "exactly 00:00:00.000" in p and "exactly 00:00:20.000" in p)
check("giới hạn từ tính theo CẢ ĐOẠN (20s × 2 từ/s = 40 từ)", "≤ 40 words" in p)
check("schema JSON mẫu đúng, có đúng 1 phần tử trong ví dụ", p.count('"id": 1') >= 1 and "EXACTLY 1 ELEMENT" in p)

p2 = build_continuous_batch_prompt(cfg, batch, 20.0, 40.0, 100.0, [ScriptSegment(1, 0.0, 20.0, "Câu trước đó.", "vui ve")])
check("có nhắc câu TRƯỚC ĐÓ để nối mạch (chỉ 1 đoạn, không phải 3 câu như chế độ cũ)", "Câu trước đó." in p2)

# ── 2) generate_script() ở chế độ continuous: Gemini giả lập LUÔN trả đúng 1 phần tử/lô ──
class FakeDriverContinuous:
    def __init__(self):
        self.prompts = []

    def ask_json(self, prompt, images, *, label=""):
        self.prompts.append(prompt)
        assert "A SINGLE CONTINUOUS FLOWING PARAGRAPH" in prompt, "phải dùng đúng prompt continuous, không lẫn prompt per_scene"
        # mô phỏng ĐÚNG Gemini thật: trả về văn liền mạch, giữ dấu câu, PHỦ ĐÚNG t0->t1 của lô
        import re
        m_t0 = re.search(r"start_time MUST be exactly (\S+),", prompt)
        m_t1 = re.search(r"end_time MUST be exactly (\S+)", prompt)
        return [{"id": 1, "start_time": m_t0.group(1), "end_time": m_t1.group(1),
                "text": "Giữa đại ngàn, anh chuẩn bị bữa tiệc. Lửa trại bập bùng, mùi thịt nướng lan tỏa khắp không gian.",
                "tone": "hao hung"}]


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    fake_video = tmp / "fake.mp4"
    fake_video.write_bytes(b"x" * 1000)   # _fingerprint() cần file THẬT tồn tại (gọi .stat()), nội dung không quan trọng
    cfg2 = Config(video_path=fake_video, output_dir=tmp / "out", narration_mode="continuous", frames_per_prompt=8,
                 words_per_sec=3.0, wps_tolerance=2.0)   # tolerance cao để câu ví dụ (ngắn) không bị cảnh báo thừa
    samples = clean_samples(16, window_len=2.5, window_step=2.5)   # 16 ảnh, 8/lô → 2 lô, mỗi lô 20s, ranh giới sạch [0,20) [20,40)

    class FakeMedia:
        duration_sec = 40.0

    driver = FakeDriverContinuous()
    segments, hook = generate_script(driver, samples, cfg2, FakeMedia(), batch_tag="chunk_cont")
    check(f"ĐÚNG 2 segment cho 2 lô (KHÔNG chia nhỏ thành nhiều câu/lô): {len(segments)}", len(segments) == 2)
    w1 = batch_window(samples, cfg2.frames_per_prompt, 0)
    w2 = batch_window(samples, cfg2.frames_per_prompt, 1)
    check(f"segment đầu bao trọn ĐÚNG cả lô 1 ({w1}), KHÔNG phải vài giây ngắn như kiểu per_scene",
          (segments[0].start_sec, segments[0].end_sec) == w1)
    check(f"segment 2 bao trọn ĐÚNG lô 2 ({w2})", (segments[1].start_sec, segments[1].end_sec) == w2)
    check("2 lô liền kề nhau (lô 2 bắt đầu đúng lúc lô 1 kết thúc, không hở/chồng)", segments[0].end_sec == segments[1].start_sec)
    check("văn bản GIỮ NGUYÊN dấu câu (phẩy, chấm) — không bị bóc tách thành câu rời",
          "," in segments[0].text and segments[0].text.count(".") >= 2)
    check("đã gọi Gemini ĐÚNG 2 lần (1 lần/lô, không gọi thêm vì nhiều câu)", len(driver.prompts) == 2)

    # ── 3) cache hoạt động bình thường — gọi lại KHÔNG hỏi Gemini lần nữa ──
    driver2 = FakeDriverContinuous()
    segments_cached, _ = generate_script(driver2, samples, cfg2, FakeMedia(), batch_tag="chunk_cont")
    check("dùng lại cache: không gọi Gemini lần nào nữa", len(driver2.prompts) == 0)
    check("kết quả từ cache giống hệt lần đầu", len(segments_cached) == 2 and segments_cached[0].text == segments[0].text)

    # ── 4) max_words_for áp dụng đúng cho đoạn DÀI (20s), không giả định đoạn ngắn ──
    mw = max_words_for(segments[0], cfg2)
    check(f"giới hạn từ tính đúng cho đoạn 20s (20×3×2=120 từ): {mw}", mw == 120)

print("TẤT CẢ PASS ✔")
