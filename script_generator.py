"""Tạo & kiểm tra kịch bản thuyết minh theo timestamp (dùng chung cho cả Gemini Web và Gemini API).

Video được chia thành các lô ảnh (frames_per_prompt ảnh/lô). Mỗi lô → một prompt (kèm ảnh có đóng dấu thời gian) →
mảng JSON các đoạn thuyết minh. Kết quả từng lô được cache để chạy lại/tiếp tục khi lỗi giữa chừng.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config
from utils import (GeminiError, MediaInfo, PipelineError, ScriptError, count_words, format_timestamp,
                   parse_timestamp, read_json, strip_accents, write_json)
from video_processor import FrameSample

log = logging.getLogger("svvc.script")

# ════════════════════════════════════════════════════════════
# Tone (giọng điệu) — dùng ở tts_engine để chọn rate/pitch/volume
# ════════════════════════════════════════════════════════════
TONES = ("vui ve", "hao hung", "tram am", "trung tinh")
_TONE_ALIASES = {
    "happy": "vui ve", "cheerful": "vui ve", "vui": "vui ve", "nhe nhang": "vui ve", "than thien": "vui ve",
    "excited": "hao hung", "energetic": "hao hung", "soi noi": "hao hung", "sinh dong": "hao hung", "kich tinh": "hao hung",
    "calm": "tram am", "deep": "tram am", "warm": "tram am", "tram": "tram am", "am ap": "tram am",
    "nghiem tuc": "tram am", "sad": "tram am", "buon": "tram am",
    "neutral": "trung tinh", "binh thuong": "trung tinh",
}


def normalize_tone(raw: Any) -> str:
    s = " ".join(strip_accents(str(raw or "")).lower().replace("_", " ").replace("-", " ").split())
    for t in TONES:
        if t in s:
            return t
    for key, target in _TONE_ALIASES.items():
        if key in s:
            return target
    return "trung tinh"


# ════════════════════════════════════════════════════════════
# Data model
# ════════════════════════════════════════════════════════════
@dataclass
class ScriptSegment:
    id: int
    start_sec: float
    end_sec: float
    text: str
    tone: str = "trung tinh"

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "start_time": format_timestamp(self.start_sec), "end_time": format_timestamp(self.end_sec),
                "duration_sec": round(self.duration_sec, 3), "text": self.text, "tone": self.tone}


_MD_RE = re.compile(r"[*_`#>~]+")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")


def clean_text(text: Any) -> str:
    return " ".join(_EMOJI_RE.sub("", _MD_RE.sub("", str(text or ""))).split())


def _coerce_segment(raw: Any) -> ScriptSegment | None:
    if not isinstance(raw, dict):
        return None
    try:
        start = parse_timestamp(raw.get("start_time", raw.get("start")))
        end = parse_timestamp(raw.get("end_time", raw.get("end")))
    except (ValueError, TypeError):
        log.warning("Bỏ qua đoạn có timestamp hỏng: %s", str(raw)[:120])
        return None
    text = clean_text(raw.get("text"))
    if not text or end <= start:
        log.warning("Bỏ qua đoạn rỗng/ngược thời gian: %s", str(raw)[:120])
        return None
    try:
        sid = int(raw.get("id") or 0)
    except (TypeError, ValueError):
        sid = 0
    return ScriptSegment(sid, start, end, text, normalize_tone(raw.get("tone", raw.get("emotion"))))


# ════════════════════════════════════════════════════════════
# Prompt
# ════════════════════════════════════════════════════════════
_STYLE_INSTRUCTIONS = {
    "natural": "",
    "humorous": "Giọng văn hài hước, dí dỏm — thỉnh thoảng chêm một câu đùa nhẹ nhàng, tự nhiên, phù hợp nội dung "
               "(không gượng ép, không lạm dụng).",
    "formal": "Giọng văn trang trọng, chuyên nghiệp — dùng từ ngữ chuẩn mực, mạch lạc, tránh khẩu ngữ/tiếng lóng.",
}


def _narrative_instructions(cfg: Config, *, is_opening_batch: bool, story_hook: str | None) -> str:
    """Trả về đoạn hướng dẫn bổ sung về NGÔI KỂ và PHONG CÁCH KỂ CHUYỆN, chèn thêm vào prompt.

    `is_opening_batch`: True khi đây là lô ảnh ĐẦU TIÊN của TOÀN VIDEO (chunk #0, lô #1) — chỉ lúc này mới
    yêu cầu mở đầu bằng câu chuyện giả tưởng. `story_hook`: nội dung mở đầu đã sinh ra trước đó (từ
    `is_opening_batch`), truyền lại cho MỌI lô sau (kể cả các chunk khác) để Gemini thỉnh thoảng nhắc lại,
    giữ mạch cảm xúc xuyên suốt cả video dài — xem `long_video_pipeline.py`, nơi giá trị này được lưu lại
    và truyền xuyên suốt các lần gọi `generate_script()` của từng chunk."""
    lines = []
    if cfg.narrator_pov.strip():
        lines.append(f"Ngôi kể/xưng hô xuyên suốt: {cfg.narrator_pov.strip()}.")

    if cfg.narrative_style == "fantasy_inspiring":
        if is_opening_batch:
            lines.append(
                "PHONG CÁCH ĐẶC BIỆT — Kể chuyện giả tưởng & Truyền cảm hứng: MỞ ĐẦU đoạn thuyết minh ĐẦU TIÊN "
                "bằng một CÂU CHUYỆN GIẢ TƯỞNG NGẮN (2-4 câu, thuộc đoạn/segment đầu tiên), giàu hình ảnh, cuốn "
                "hút, gợi cảm xúc — có thể là một tình huống, nhân vật hoặc thế giới tưởng tượng LIÊN QUAN tới "
                "chủ đề video — dùng để DẪN DẮT người xem vào nội dung chính. Ngay sau đó, CHUYỂN TỰ NHIÊN sang "
                "thuyết minh nội dung THẬT của video (bám sát hình ảnh như bình thường) — câu chuyện giả tưởng "
                "chỉ là phần mở màn, không được bịa thêm nội dung không có trong hình ảnh ở các đoạn sau.")
        elif story_hook:
            lines.append(
                "PHONG CÁCH ĐẶC BIỆT — Kể chuyện giả tưởng & Truyền cảm hứng: video này đã MỞ ĐẦU bằng câu "
                f"chuyện giả tưởng sau: “{story_hook}”. THỈNH THOẢNG (không phải ở mọi đoạn, chỉ vài chỗ hợp lý "
                "trong lô này) hãy LỒNG GHÉP hoặc NHẮC KHÉO LẠI một chi tiết/hình ảnh/cảm xúc từ câu chuyện đó "
                "để giữ mạch cảm hứng xuyên suốt — không lặp lại y nguyên, chỉ liên hệ ngắn gọn, tự nhiên, "
                "không được làm gián đoạn việc bám sát nội dung hình ảnh thực tế.")
    else:
        extra = _STYLE_INSTRUCTIONS.get(cfg.narrative_style, "")
        if extra:
            lines.append(extra)
    return ("\n" + "\n".join(lines)) if lines else ""


def build_batch_prompt(cfg: Config, batch: list[FrameSample], t0: float, t1: float, video_duration: float,
                       previous: list[ScriptSegment], *, is_opening_batch: bool = False,
                       story_hook: str | None = None) -> str:
    wps = cfg.words_per_sec
    example = [{"id": 1, "start_time": "00:00:01.000", "end_time": "00:00:05.500", "duration_sec": 4.5,
                "text": "Lời thuyết minh khớp với phân cảnh...", "tone": "hao hung"}]
    frames = "\n".join(f"  Ảnh {i}: {format_timestamp(s.timestamp_sec)}" for i, s in enumerate(batch, start=1))
    prompt = (
        f"Bạn là biên kịch lồng tiếng chuyên nghiệp. Tôi gửi kèm {len(batch)} ảnh được trích LIÊN TIẾP từ một video KHÔNG LỜI "
        f"(tổng thời lượng {format_timestamp(video_duration)}). Góc trên bên trái mỗi ảnh có nhãn thời gian HH:MM:SS.mmm — "
        "đó là thời điểm của ảnh trong video.\n"
        f"Thứ tự ảnh đính kèm:\n{frames}\n\n"
        f"NHIỆM VỤ: viết lời thuyết minh bằng {cfg.language_name}, phong cách: {cfg.style}, cho đoạn video từ "
        f"{format_timestamp(t0)} đến {format_timestamp(t1)}. So sánh các ảnh liên tiếp để hiểu HÀNH ĐỘNG đang diễn ra."
        f"{_narrative_instructions(cfg, is_opening_batch=is_opening_batch, story_hook=story_hook)}\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "1. Chỉ trả về MỘT khối mã ```json ... ``` duy nhất chứa mảng JSON theo đúng mẫu (không thêm lời dẫn/giải thích):\n"
        f"```json\n{json.dumps(example, ensure_ascii=False, indent=2)}\n```\n"
        "2. start_time/end_time dạng HH:MM:SS.mmm; duration_sec = end_time − start_time.\n"
        f"3. Tốc độ đọc ≈ {wps:g} từ/giây (tiếng Việt: mỗi tiếng ngăn cách bằng khoảng trắng là 1 từ). Số từ của `text` PHẢI ≤ "
        f"duration_sec × {wps:g} (ví dụ đoạn 4 giây tối đa {int(4 * wps)} từ). Thà ngắn còn hơn quá dài.\n"
        f"4. Chỉ dùng thời gian trong [{format_timestamp(t0)}, {format_timestamp(t1)}]; các đoạn nối tiếp, KHÔNG chồng lấn, "
        f"chừa ≥ 0.2 giây giữa hai đoạn, mỗi đoạn dài {cfg.min_segment_sec:g}–10 giây và bám sát mốc thời gian của hình ảnh.\n"
        "5. Bám sát hình ảnh, không bịa chi tiết; đoạn tĩnh/không có gì đáng nói thì bỏ qua.\n"
        "6. Văn nói tự nhiên, truyền cảm, liền mạch; không emoji, ký hiệu, ngoặc, viết tắt khó đọc; số viết thành chữ khi cần.\n"
        '7. tone chỉ được là một trong: "vui ve", "hao hung", "tram am", "trung tinh" (không dấu).\n'
        "8. id đánh số tăng dần từ 1."
    )
    if previous:
        prompt += "\n\nCác câu thuyết minh ngay trước đó (để nối mạch, KHÔNG lặp lại):\n" + "\n".join(f"- {s.text}" for s in previous[-3:])
    return prompt


# ════════════════════════════════════════════════════════════
# Chế độ LINK YOUTUBE — gửi thẳng URL cho Gemini Web, KHÔNG trích frame/đính kèm ảnh cục bộ. Gemini tự
# "xem" video từ link (đã xác nhận: dán link YouTube vào khung chat Gemini khiến nó đọc được nội dung
# video, xem UPGRADE_LONG_VIDEO.md/README để biết thêm) — nên mỗi macro-chunk chỉ cần ĐÚNG MỘT lượt hỏi
# (không cần chia thành nhiều "lô ảnh" như chế độ video cục bộ), nhanh hơn hẳn vì bỏ được: trích frame
# OpenCV, mã hoá JPEG, và đặc biệt là thao tác tải ảnh lên qua Playwright (vốn khá chậm, thấy rõ trong log
# chạy thật) — chỉ còn lại một tin nhắn văn bản.
# ════════════════════════════════════════════════════════════
def build_youtube_prompt(cfg: Config, youtube_url: str, t0: float, t1: float, video_duration: float,
                         previous: list[ScriptSegment], *, is_opening_batch: bool = False,
                         story_hook: str | None = None) -> str:
    wps = cfg.words_per_sec
    example = [{"id": 1, "start_time": "00:00:01.000", "end_time": "00:00:05.500", "duration_sec": 4.5,
                "text": "Lời thuyết minh khớp với phân cảnh...", "tone": "hao hung"}]
    prompt = (
        f"Đây là link video YouTube: {youtube_url}\n"
        f"Video này KHÔNG có lời thoại/thuyết minh (silent/gameplay/B-roll...), tổng thời lượng "
        f"{format_timestamp(video_duration)}. HÃY XEM kỹ đoạn từ {format_timestamp(t0)} đến {format_timestamp(t1)} "
        "của video này (bỏ qua các phần khác) — chú ý cả HÌNH ẢNH lẫn diễn biến theo thời gian.\n\n"
        f"NHIỆM VỤ: viết lời thuyết minh bằng {cfg.language_name}, phong cách: {cfg.style}, cho ĐÚNG đoạn "
        f"[{format_timestamp(t0)}, {format_timestamp(t1)}] vừa nêu, bám sát hành động/nội dung thật đang diễn ra."
        f"{_narrative_instructions(cfg, is_opening_batch=is_opening_batch, story_hook=story_hook)}\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "1. Chỉ trả về MỘT khối mã ```json ... ``` duy nhất chứa mảng JSON theo đúng mẫu (không thêm lời dẫn/giải thích):\n"
        f"```json\n{json.dumps(example, ensure_ascii=False, indent=2)}\n```\n"
        "2. start_time/end_time dạng HH:MM:SS.mmm (mốc thời gian THẬT trong video, không phải mốc riêng của đoạn "
        "trích); duration_sec = end_time − start_time.\n"
        f"3. Tốc độ đọc ≈ {wps:g} từ/giây (tiếng Việt: mỗi tiếng ngăn cách bằng khoảng trắng là 1 từ). Số từ của "
        f"`text` PHẢI ≤ duration_sec × {wps:g} (ví dụ đoạn 4 giây tối đa {int(4 * wps)} từ). Thà ngắn còn hơn quá dài.\n"
        f"4. CHỈ dùng thời gian trong [{format_timestamp(t0)}, {format_timestamp(t1)}]; các đoạn nối tiếp, KHÔNG "
        f"chồng lấn, chừa ≥ 0.2 giây giữa hai đoạn, mỗi đoạn dài {cfg.min_segment_sec:g}–10 giây.\n"
        "5. Bám sát nội dung THẬT của video, không bịa chi tiết; đoạn tĩnh/không có gì đáng nói thì bỏ qua.\n"
        "6. Văn nói tự nhiên, truyền cảm, liền mạch; không emoji, ký hiệu, ngoặc, viết tắt khó đọc; số viết thành "
        "chữ khi cần.\n"
        '7. tone chỉ được là một trong: "vui ve", "hao hung", "tram am", "trung tinh" (không dấu).\n'
        "8. id đánh số tăng dần từ 1.\n"
        "9. Nếu KHÔNG xem được nội dung video từ link này (video riêng tư/đã gỡ/không truy cập được), trả về "
        'CHÍNH XÁC: ```json\n[]\n```\n và đừng đoán mò/bịa nội dung.'
    )
    if previous:
        prompt += "\n\nCác câu thuyết minh ngay trước đó (để nối mạch, KHÔNG lặp lại):\n" + "\n".join(f"- {s.text}" for s in previous[-3:])
    return prompt


def generate_script_from_youtube(driver: Any, cfg: Config, youtube_url: str, t0: float, t1: float,
                                 video_duration: float, *, batch_tag: str = "yt", is_first_chunk: bool = False,
                                 story_hook: str | None = None) -> tuple[list[ScriptSegment], str | None]:
    """Tương đương `generate_script()` nhưng cho MỘT macro-chunk theo link YouTube — chỉ MỘT lượt hỏi Gemini
    (không chia lô ảnh vì không có ảnh nào), kết quả được cache theo (link + khoảng thời gian) y hệt cơ chế
    cache theo lô ảnh của chế độ video cục bộ (đổi link/khoảng thời gian → tự tạo lại, không dùng nhầm)."""
    fp = {"youtube_url": youtube_url, "range": [round(t0, 3), round(t1, 3)], "language": cfg.language,
         "style": cfg.style, "narrator_pov": cfg.narrator_pov, "narrative_style": cfg.narrative_style,
         "wps": cfg.words_per_sec}
    cfg.batches_dir.mkdir(parents=True, exist_ok=True)
    cache = cfg.batches_dir / f"{batch_tag}_001.json"
    if cfg.force:
        cache.unlink(missing_ok=True)

    raw: list[dict] | None = None
    if cache.is_file():
        try:
            data = read_json(cache)
            if data.get("fingerprint") == fp:
                raw = data["segments"]
                log.info("(%s → %s): dùng lại kết quả đã lưu.", format_timestamp(t0), format_timestamp(t1))
        except (PipelineError, KeyError, TypeError):
            raw = None

    story_hook_out = story_hook
    if raw is None:
        log.info("(%s → %s): gửi link YouTube cho Gemini Web (không cần trích frame/tải ảnh)...",
                 format_timestamp(t0), format_timestamp(t1))
        opening = is_first_chunk and cfg.narrative_style == "fantasy_inspiring"
        prompt = build_youtube_prompt(cfg, youtube_url, t0, t1, video_duration, [], is_opening_batch=opening,
                                      story_hook=story_hook)
        raw = driver.ask_json(prompt, [], label=batch_tag)
        write_json(cache, {"fingerprint": fp, "segments": raw})
        if opening and raw:
            hook_text = " ".join(str(item.get("text", "")) for item in raw[:2])
            story_hook_out = (hook_text[:280] + "…") if len(hook_text) > 280 else hook_text
            log.info("Đã trích câu chuyện mở đầu (phong cách 'fantasy_inspiring'): %s", story_hook_out)

    segments: list[ScriptSegment] = []
    for item in raw:
        seg = _coerce_segment(item)
        if seg is None or seg.end_sec <= t0 or seg.start_sec >= t1:
            continue
        seg.start_sec, seg.end_sec = max(seg.start_sec, t0), min(seg.end_sec, t1)
        segments.append(seg)
    log.info("  → %d đoạn thuyết minh.", len(segments))
    if not segments:
        log.warning("Chunk [%s → %s] (link YouTube): Gemini không trả về đoạn nào hợp lệ — video có thể không "
                    "xem được từ link này (riêng tư/bị hạn chế) hoặc đoạn này không có gì đáng thuyết minh.",
                    format_timestamp(t0), format_timestamp(t1))
    return segments, story_hook_out


# ════════════════════════════════════════════════════════════
# Sinh kịch bản theo lô (có cache)
# ════════════════════════════════════════════════════════════
def plan_batches(samples: list[FrameSample], per_prompt: int) -> list[list[FrameSample]]:
    return [samples[i:i + per_prompt] for i in range(0, len(samples), per_prompt)]


def _fingerprint(cfg: Config, media: MediaInfo) -> dict[str, Any]:
    return {"video": cfg.video_path.name, "size": cfg.video_path.stat().st_size, "duration": round(media.duration_sec, 2),
            "mode": cfg.extract_mode, "interval": cfg.frame_interval_sec, "per_prompt": cfg.frames_per_prompt,
            "language": cfg.language, "style": cfg.style, "wps": cfg.words_per_sec}


def generate_script(driver: Any, samples: list[FrameSample], cfg: Config, media: MediaInfo, *,
                    batch_tag: str = "batch", context_duration_sec: float | None = None,
                    is_first_chunk: bool = False, story_hook: str | None = None
                    ) -> tuple[list[ScriptSegment], str | None]:
    """`batch_tag`: tiền tố đặt tên cache/label của từng lô prompt — khi xử lý video dài theo từng
    MACRO-CHUNK (long_video_pipeline.py), mỗi chunk truyền một `batch_tag` riêng (vd 'chunk0007') để cache
    của các chunk không ghi đè lẫn nhau trong cùng `batches_dir`.
    `context_duration_sec`: độ dài video nhắc trong prompt cho Gemini biết bối cảnh tổng thể (mặc định dùng
    đúng độ dài của toàn bộ video, kể cả khi `samples` chỉ thuộc một chunk).
    `is_first_chunk`/`story_hook`: dùng cho phong cách "fantasy_inspiring" — `is_first_chunk=True` (chỉ
    đúng ở macro-chunk #0) yêu cầu Gemini mở đầu bằng câu chuyện giả tưởng ở LÔ ĐẦU TIÊN; hàm này sau đó
    TỰ TRÍCH lại nội dung mở đầu đó và trả về qua giá trị `story_hook` thứ hai (khác `None` truyền vào) để
    `long_video_pipeline.py` mang tiếp qua các chunk SAU, giúp Gemini thỉnh thoảng nhắc lại xuyên suốt cả
    video dài — chỉ trích MỘT LẦN DUY NHẤT (lô 1 của chunk 0); các lần gọi sau nhận `story_hook` có sẵn và
    CHỈ TRUYỀN LẠI NGUYÊN VẸN, không trích lại."""
    batches = plan_batches(samples, cfg.frames_per_prompt)
    fp = _fingerprint(cfg, media)
    cfg.batches_dir.mkdir(parents=True, exist_ok=True)
    if cfg.force:
        for f in cfg.batches_dir.glob(f"{batch_tag}_*.json"):
            f.unlink(missing_ok=True)
    segments: list[ScriptSegment] = []
    video_duration = context_duration_sec if context_duration_sec is not None else media.duration_sec

    for bi, batch in enumerate(batches, start=1):
        t0, t1 = batch[0].window_start, batch[-1].window_end
        cache = cfg.batches_dir / f"{batch_tag}_{bi:03d}.json"
        opening = is_first_chunk and bi == 1 and cfg.narrative_style == "fantasy_inspiring"
        raw: list[dict] | None = None
        if cache.is_file():
            try:
                data = read_json(cache)
                if data.get("fingerprint") == fp and data.get("range") == [round(t0, 3), round(t1, 3)]:
                    raw = data["segments"]
                    log.info("Lô %d/%d (%s → %s): dùng lại kết quả đã lưu.", bi, len(batches), format_timestamp(t0), format_timestamp(t1))
            except (PipelineError, KeyError, TypeError):
                raw = None
        if raw is None:
            log.info("Lô %d/%d (%s → %s, %d ảnh): gửi %s...", bi, len(batches), format_timestamp(t0), format_timestamp(t1),
                     len(batch), "Gemini Web" if cfg.engine == "web" else "Gemini API")
            prompt = build_batch_prompt(cfg, batch, t0, t1, video_duration, segments,
                                        is_opening_batch=opening, story_hook=story_hook)
            raw = driver.ask_json(prompt, [s.path for s in batch], label=f"{batch_tag}-{bi}")
            write_json(cache, {"fingerprint": fp, "range": [round(t0, 3), round(t1, 3)], "segments": raw})

        got = 0
        batch_start_idx = len(segments)
        for item in raw:
            seg = _coerce_segment(item)
            if seg is None or seg.end_sec <= t0 or seg.start_sec >= t1:
                continue
            seg.start_sec, seg.end_sec = max(seg.start_sec, t0), min(seg.end_sec, t1)
            segments.append(seg)
            got += 1
        log.info("  → %d đoạn thuyết minh.", got)

        if opening and story_hook is None and got > 0:
            hook_text = " ".join(s.text for s in segments[batch_start_idx:batch_start_idx + 2])
            story_hook = (hook_text[:280] + "…") if len(hook_text) > 280 else hook_text
            log.info("Đã trích câu chuyện mở đầu (phong cách 'fantasy_inspiring') để nhắc lại xuyên suốt video: %s",
                     story_hook)

    if not segments:
        raise ScriptError("Không tạo được đoạn thuyết minh hợp lệ nào. Thử --force, khoảng lấy mẫu khác hoặc engine khác.")
    return segments, story_hook


# ════════════════════════════════════════════════════════════
# Kiểm tra & sửa
# ════════════════════════════════════════════════════════════
def max_words_for(seg: ScriptSegment, cfg: Config) -> int:
    return max(1, int(seg.duration_sec * cfg.words_per_sec * cfg.wps_tolerance))


def validate_and_fix(segments: list[ScriptSegment], cfg: Config, video_duration: float, driver: Any | None,
                     auto_shorten: bool = True, min_time: float = 0.0) -> list[ScriptSegment]:
    """`min_time`/`video_duration`: biên dưới/trên hợp lệ cho start_sec/end_sec. Với video ngắn (1 chunk
    bao trọn) đây là [0, độ dài video]; khi xử lý theo từng MACRO-CHUNK (long_video_pipeline.py), đây là
    [chunk.start_sec, chunk.end_sec] — QUAN TRỌNG: không được để mặc định min_time=0.0 cho một chunk giữa
    video, nếu không một đoạn bị lệch nhẹ về trước có thể bị kẹp giá trị start_sec=0 và "nuốt" luôn toàn bộ
    phần video trước chunk đó khi ghép lên timeline."""
    fixed: list[ScriptSegment] = []
    prev_end = min_time
    for s in sorted(segments, key=lambda x: x.start_sec):
        s.start_sec, s.end_sec = max(min_time, s.start_sec), min(s.end_sec, video_duration)
        if s.start_sec < prev_end - 1e-3:
            log.warning("Đoạn %s chồng lấn đoạn trước (%s < %s) → dời điểm bắt đầu.", s.id or "?",
                        format_timestamp(s.start_sec), format_timestamp(prev_end))
            s.start_sec = prev_end
        if s.duration_sec < cfg.min_segment_sec:
            log.warning("Loại đoạn quá ngắn (%.2fs) tại %s: %r", s.duration_sec, format_timestamp(s.start_sec), s.text[:50])
            continue
        fixed.append(s)
        prev_end = s.end_sec
    if not fixed:
        raise ScriptError("Kịch bản rỗng sau khi kiểm tra timeline.")
    for i, s in enumerate(fixed, start=1):
        s.id = i

    if auto_shorten and driver is not None:
        for round_no in range(1, cfg.max_rewrite_rounds + 1):
            too_long = [s for s in fixed if count_words(s.text) > max_words_for(s, cfg)]
            if not too_long:
                break
            log.warning("Vòng %d: %d/%d đoạn quá dài so với thời lượng → nhờ Gemini rút gọn.", round_no, len(too_long), len(fixed))
            try:
                _shorten(driver, too_long, cfg, round_no)
            except (GeminiError, PipelineError) as e:
                log.error("Rút gọn bằng Gemini thất bại: %s", e)
                break
        for s in fixed:
            limit = max_words_for(s, cfg)
            if count_words(s.text) > limit:
                old = count_words(s.text)
                s.text = _truncate_words(s.text, limit)
                log.warning("Đoạn %d vẫn dài (%d > %d từ) → cắt còn %d từ.", s.id, old, limit, count_words(s.text))

    for s in fixed:
        words, limit = count_words(s.text), max_words_for(s, cfg)
        if words > limit:
            log.warning("Đoạn %d: %d từ / %.2fs = %.1f từ/giây (> %.1f) — sẽ phải ép tốc độ ở bước TTS.",
                        s.id, words, s.duration_sec, words / s.duration_sec, limit / s.duration_sec)
        else:
            log.debug("Đoạn %d: %d từ / %.2fs = %.1f từ/giây ✓", s.id, words, s.duration_sec, words / s.duration_sec)
    covered = sum(s.duration_sec for s in fixed)
    log.info("Kịch bản: %d đoạn, phủ %.0f%% video (%.1fs/%.1fs).", len(fixed), 100 * covered / max(video_duration, 1e-6),
             covered, video_duration)
    return fixed


def _shorten(driver: Any, too_long: list[ScriptSegment], cfg: Config, round_no: int) -> None:
    payload = [{"id": s.id, "duration_sec": round(s.duration_sec, 2), "max_words": max_words_for(s, cfg), "text": s.text}
               for s in too_long]
    prompt = (f"Bạn là biên tập viên lồng tiếng. Rút gọn TỪNG lời thuyết minh ({cfg.language_name}) dưới đây sao cho số từ ≤ max_words, "
              "giữ ý chính, văn nói tự nhiên, không đổi ngôn ngữ, không thêm ký hiệu.\n"
              'Chỉ trả về MỘT khối mã ```json ... ``` chứa mảng [{"id": <id>, "text": "<lời đã rút gọn>"}] cho đúng các id sau:\n'
              f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```")
    raw = driver.ask_json(prompt, [], label=f"shorten-{round_no}")
    by_id = {s.id: s for s in too_long}
    for item in raw:
        try:
            seg = by_id.get(int(item.get("id")))
        except (TypeError, ValueError, AttributeError):
            continue
        new_text = clean_text(item.get("text"))
        if seg and new_text and count_words(new_text) < count_words(seg.text):
            seg.text = new_text


def _truncate_words(text: str, limit: int) -> str:
    tokens = text.split()
    while tokens and count_words(" ".join(tokens)) > limit:
        tokens.pop()
    cut = " ".join(tokens)
    m = list(re.finditer(r"[.!?…,;:]", cut))
    if m and m[-1].end() >= len(cut) * 0.6:
        cut = cut[:m[-1].end()]
    cut = cut.rstrip(" ,;:-")
    return cut + ("" if cut.endswith((".", "!", "?", "…")) else ".")


# ════════════════════════════════════════════════════════════
# I/O
# ════════════════════════════════════════════════════════════
def save_script(path: Path, segments: list[ScriptSegment]) -> None:
    write_json(path, [s.to_json() for s in segments])
    log.info("Đã lưu kịch bản → %s", path)


def load_script_file(path: Path) -> list[ScriptSegment]:
    data = read_json(path)
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        raise ScriptError(f"{path.name}: gốc JSON phải là một mảng các đoạn thuyết minh.")
    segs = [s for s in (_coerce_segment(r) for r in data) if s]
    if not segs:
        raise ScriptError(f"{path.name}: không có đoạn hợp lệ nào (cần start_time, end_time, text).")
    return segs


def try_load_existing_script(cfg: Config, media: MediaInfo) -> list[ScriptSegment] | None:
    """--script-file hoặc script.json đã có (chưa --force) → dùng lại, bỏ qua bước đăng nhập/phân tích/tạo kịch bản."""
    source: Path | None = cfg.script_file or (cfg.script_path if cfg.script_path.is_file() and not cfg.force else None)
    if source is None:
        return None
    try:
        segs = load_script_file(source)
    except PipelineError as e:
        if cfg.script_file:
            raise
        log.warning("script.json có sẵn không dùng được (%s) → tạo mới.", e)
        return None
    log.info("Dùng kịch bản có sẵn: %s (%d đoạn). Thêm --force để tạo lại từ đầu.", source, len(segs))
    return validate_and_fix(segs, cfg, media.duration_sec, None, auto_shorten=False)
