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
    certainty: str = "observed_fact"   # "observed_fact" | "safe_inference" | "creative_framing" |
                                       # "user_provided_fact" — THÊM Ở CUỐI (có giá trị mặc định) để KHÔNG
                                       # phá các lời gọi ScriptSegment(id, start, end, text, tone) đã có khắp
                                       # codebase theo kiểu tham số vị trí. Mặc định "observed_fact" (mức
                                       # chặt nhất) khi không rõ — an toàn hơn là mặc định mức lỏng.

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "start_time": format_timestamp(self.start_sec), "end_time": format_timestamp(self.end_sec),
                "duration_sec": round(self.duration_sec, 3), "text": self.text, "tone": self.tone,
                "certainty": self.certainty}


_MD_RE = re.compile(r"[*_`#>~]+")
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")


def clean_text(text: Any) -> str:
    return " ".join(_EMOJI_RE.sub("", _MD_RE.sub("", str(text or ""))).split())


CERTAINTY_LEVELS = ("observed_fact", "safe_inference", "creative_framing", "user_provided_fact")


def normalize_certainty(raw: Any) -> str:
    """Chuẩn hoá nhãn `certainty` Gemini trả về — khác `normalize_tone()` ở chỗ CHỈ chấp nhận ĐÚNG 1 trong 4
    giá trị cố định (không dò theo từ khoá gần đúng, vì đây là nhãn kỹ thuật dùng để kiểm tra chất lượng,
    sai lệch ở đây nguy hiểm hơn sai lệch giọng điệu `tone`) — bất kỳ giá trị lạ/thiếu nào đều rơi về
    "observed_fact" (mức CHẶT NHẤT), không bao giờ tự nới lỏng khi không chắc chắn."""
    s = str(raw or "").strip().lower()
    return s if s in CERTAINTY_LEVELS else "observed_fact"


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
    return ScriptSegment(sid, start, end, text, normalize_tone(raw.get("tone", raw.get("emotion"))),
                        normalize_certainty(raw.get("certainty")))


# ════════════════════════════════════════════════════════════
# Prompt
# ════════════════════════════════════════════════════════════
_STYLE_INSTRUCTIONS = {
    "natural": "",
    "humorous": "Humorous, witty tone — occasionally slip in a light, natural joke that fits the content "
               "(never forced, never overused).",
    "formal": "Formal, professional tone — precise, coherent wording; avoid colloquialisms/slang.",
}

# "Narration purpose" — DIFFERENT video purposes need completely different narration approaches (e.g.
# surveillance/incident footage needs neutral, objective description; an ad needs a persuasive tone) —
# independent of `narrative_style` (which only decides TONE: humorous/formal/natural). Matches *at minimum*
# (no need for all 8 at once) the "narration purpose" list already discussed — key names are INTENTIONALLY
# short, 1-1 with the GUI choices.
_PURPOSE_INSTRUCTIONS = {
    "neutral": "PURPOSE: NEUTRAL, objective description of what's happening — no added emotion/personal "
              "commentary, no speculating about intent/cause if the image isn't clear.",
    "documentary": "PURPOSE: DOCUMENTARY-style storytelling — calm, substantive tone, placing events in "
                  "broader context when reasonable, but still grounded in visual evidence, no unsupported "
                  "speculation.",
    "emotional": "PURPOSE: EMOTION-rich storytelling — choose words and sentence rhythm that evoke feeling, "
                "connecting the viewer to the moment in the video, but do NOT invent details just to "
                "manufacture emotion.",
    "educational": "PURPOSE: EXPLANATORY/EDUCATIONAL content — prioritize clarity, easy to understand, may "
                  "add background knowledge directly relevant to what's visible on screen.",
    "review": "PURPOSE: REVIEW/ANALYSIS — give specific commentary/assessment of what's shown (quality, "
             "pros/cons...), an opinionated tone that still stays grounded in what the visuals actually show.",
    "ad": "PURPOSE: ADVERTISING a product/service — persuasive tone, highlight the standout points visible "
         "on screen, tight pacing, a soft call-to-action near the end if it fits naturally.",
    "social_short": "PURPOSE: SHORT SOCIAL-MEDIA video — very short sentences, fast pace, get straight to "
                   "the point from the first sentence, no long preamble.",
    "light_humor": "PURPOSE: LIGHT HUMOR — witty tone, notice subtle funny/charming details in the footage, "
                  "never forced, never mocking.",
}


def _creativity_instruction(level: float) -> str:
    """Mức độ sáng tạo (0.0 bám sát hình ảnh ↔ 1.0 sáng tác tự do) — 3 mốc, mỗi mốc kèm VÍ DỤ cụ thể để
    Gemini hiểu đúng SẮC THÁI khác biệt (chỉ mô tả bằng lời dễ bị hiểu mơ hồ hơn nhiều so với có ví dụ).
    NỘI DUNG PROMPT đã dịch sang tiếng Anh để tối ưu (docstring này giữ tiếng Việt cho người đọc code)."""
    if level <= 0.35:
        return ("CREATIVITY LEVEL: LOW — ONLY describe what's directly observed, neutrally. Do NOT infer "
                "characters' intent/emotions, do NOT add details with no evidence in the image. Example AT "
                "this level: \"A person walks along the beach as the morning light falls over the water.\"")
    if level <= 0.75:
        return ("CREATIVITY LEVEL: MEDIUM — may add emotion/rhythm through WORD CHOICE and PHRASING, but "
                "must NOT invent new events/specific details not present in the image. Example AT this "
                "level: \"The pace slows, as if this morning were made for noticing rather than rushing "
                "through.\" — still describes ONLY the actual observed action (walking slowly), just in more "
                "vivid language, with NO new event added.")
    return ("CREATIVITY LEVEL: HIGH — may write in a literary, evocative, more interpretive way grounded in "
            "direct evidence — but ABSOLUTELY MUST NOT invent specific names of people/places, dates, "
            "occupations, or claim this is a real-world event/person if the image doesn't prove it — the "
            "creative license applies only to HOW it's told, never to FABRICATING information that looks "
            "like fact.")


def _narrative_instructions(cfg: Config, *, is_opening_batch: bool, story_hook: str | None,
                            chunk_outline: str | None = None) -> str:
    """Trả về đoạn hướng dẫn bổ sung về NGÔI KỂ và PHONG CÁCH KỂ CHUYỆN, chèn thêm vào prompt.

    `is_opening_batch`: True khi đây là lô ảnh ĐẦU TIÊN của TOÀN VIDEO (chunk #0, lô #1) — chỉ lúc này mới
    yêu cầu mở đầu bằng câu chuyện giả tưởng. `story_hook`: nội dung mở đầu đã sinh ra trước đó (từ
    `is_opening_batch`), truyền lại cho MỌI lô sau (kể cả các chunk khác) để Gemini thỉnh thoảng nhắc lại,
    giữ mạch cảm xúc xuyên suốt cả video dài — xem `long_video_pipeline.py`, nơi giá trị này được lưu lại
    và truyền xuyên suốt các lần gọi `generate_script()` của từng chunk.
    `chunk_outline`: dàn ý tổng thể của CẢ CHUNK (từ `plan_chunk_outline()`, chỉ có khi
    `cfg.enable_story_plan=True`) — GIỐNG NHAU cho mọi lô trong cùng 1 chunk (khác `story_hook` ở chỗ đó chỉ
    sinh 1 lần đầu video; `chunk_outline` sinh lại cho MỖI chunk, phản ánh đúng bối cảnh RIÊNG của chunk đó)."""
    lines = []
    if chunk_outline:
        lines.append(f"OVERALL OUTLINE for this video segment (to get the hook/development/climax/ending arc "
                     f"right — use ONLY to GUIDE the storytelling, do not repeat it verbatim): {chunk_outline}")
    if cfg.narrator_pov.strip():
        lines.append(f"Narrator point of view, consistent throughout: {cfg.narrator_pov.strip()}.")

    purpose = _PURPOSE_INSTRUCTIONS.get(cfg.narration_purpose, "")
    if purpose:
        lines.append(purpose)
    lines.append(_creativity_instruction(cfg.creativity_level))

    if cfg.narrative_style == "fantasy_inspiring":
        if is_opening_batch:
            lines.append(
                "SPECIAL STYLE — Fantastical & Inspiring storytelling: OPEN the very FIRST narration segment "
                "with a SHORT FANTASTICAL STORY (2-4 sentences, part of the first segment), vivid, engaging, "
                "evocative — it may be a scenario, character, or imagined world RELATED to the video's theme — "
                "used to DRAW the viewer into the main content. Right after that, transition NATURALLY into "
                "narrating the video's REAL content (grounded in the visuals as usual) — the fantastical story "
                "is only the opening hook; later segments must not invent content absent from the visuals.")
        elif story_hook:
            lines.append(
                "SPECIAL STYLE — Fantastical & Inspiring storytelling: this video already OPENED with this "
                f"fantastical story: “{story_hook}”. OCCASIONALLY (not every segment, just a few fitting spots "
                "in this batch) WEAVE IN or SUBTLY CALL BACK to a detail/image/feeling from that story to keep "
                "the inspirational thread alive — don't repeat it verbatim, just a brief, natural callback — "
                "must not interrupt staying grounded in the actual visual content.")
    else:
        extra = _STYLE_INSTRUCTIONS.get(cfg.narrative_style, "")
        if extra:
            lines.append(extra)
    return ("\n" + "\n".join(lines)) if lines else ""


_CERTAINTY_RULE = (
    'certainty MUST be ONE of 4 values: "observed_fact" (describes exactly what is DIRECTLY SEEN, no '
    'inference — e.g. "a person is riding a bicycle"), "safe_inference" (a reasonable, GROUNDED inference '
    'from the image but not stated as certain — e.g. "it appears to be a quiet morning"), "creative_framing" '
    '(a literary/evocative way of phrasing, with NO new specific event added), "user_provided_fact" (ONLY '
    'when the information comes from user-supplied data, not inferred from the image). ABSOLUTELY DO NOT '
    'invent specific names of people/places, dates, occupations, or claim a real-world event if the image '
    'does not prove it — if unsure, choose "safe_inference" or write a more neutral sentence; NEVER guess '
    'and then label it "observed_fact".'
)


def build_batch_prompt(cfg: Config, batch: list[FrameSample], t0: float, t1: float, video_duration: float,
                       previous: list[ScriptSegment], *, is_opening_batch: bool = False,
                       story_hook: str | None = None, chunk_outline: str | None = None) -> str:
    wps = cfg.words_per_sec
    example = [{"id": 1, "start_time": "00:00:01.000", "end_time": "00:00:05.500", "duration_sec": 4.5,
                "text": "Narration matching the scene...", "tone": "hao hung", "certainty": "observed_fact"}]
    frames = "\n".join(f"  Image {i}: {format_timestamp(s.timestamp_sec)}" for i, s in enumerate(batch, start=1))
    prompt = (
        f"You are a professional voice-over scriptwriter. I'm attaching {len(batch)} images extracted "
        f"CONSECUTIVELY from a SILENT video (total duration {format_timestamp(video_duration)}). The top-left "
        "corner of each image has a timestamp label HH:MM:SS.mmm — that's the image's position in the video.\n"
        f"Order of attached images:\n{frames}\n\n"
        f"TASK: write narration in {cfg.language_name}, style: {cfg.style}, for the video segment from "
        f"{format_timestamp(t0)} to {format_timestamp(t1)}. Compare consecutive images to understand the "
        "ACTION taking place."
        f"{_narrative_instructions(cfg, is_opening_batch=is_opening_batch, story_hook=story_hook, chunk_outline=chunk_outline)}\n\n"
        "MANDATORY RULES:\n"
        "1. Return ONLY ONE ```json ... ``` code block containing a JSON array in this exact shape (no preamble/explanation):\n"
        f"```json\n{json.dumps(example, ensure_ascii=False, indent=2)}\n```\n"
        "2. start_time/end_time in HH:MM:SS.mmm format; duration_sec = end_time − start_time.\n"
        f"3. Speaking rate ≈ {wps:g} words/sec (word count = number of whitespace-separated tokens — in "
        f"Vietnamese that means each syllable counts as one word, not each compound word). The word count of "
        f"`text` MUST be ≤ duration_sec × {wps:g} (e.g. a 4-second segment gets at most {int(4 * wps)} words). "
        "Shorter is better than too long.\n"
        f"4. Only use timestamps within [{format_timestamp(t0)}, {format_timestamp(t1)}]; segments must be "
        f"sequential, NOT overlapping, with ≥ 0.2s gap between two segments, each segment {cfg.min_segment_sec:g}"
        "–10 seconds long and tightly timed to the visual action.\n"
        "5. Stay grounded in the visuals, don't invent details; skip a segment if the footage is static/has "
        "nothing worth narrating.\n"
        "6. Natural, expressive, flowing spoken language; no emoji, symbols, brackets, hard-to-read "
        "abbreviations; spell out numbers as words when needed.\n"
        '7. tone must be exactly one of: "vui ve", "hao hung", "tram am", "trung tinh" (these are fixed '
        "internal Vietnamese labels — keep them exactly as written, do not translate them).\n"
        "8. id numbered sequentially starting from 1.\n"
        f"9. {_CERTAINTY_RULE}"
    )
    if previous:
        prompt += "\n\nThe narration lines immediately before this (for continuity, do NOT repeat them):\n" + \
            "\n".join(f"- {s.text}" for s in previous[-3:])
    return prompt


def build_continuous_batch_prompt(cfg: Config, batch: list[FrameSample], t0: float, t1: float, video_duration: float,
                                  previous: list[ScriptSegment], *, is_opening_batch: bool = False,
                                  story_hook: str | None = None, chunk_outline: str | None = None) -> str:
    """Chế độ "Đọc liên tục" (`cfg.narration_mode == "continuous"`) — THAY VÌ yêu cầu Gemini chia nhỏ 1 lô
    ảnh thành nhiều câu rời (mỗi câu tự mở/đóng ngữ điệu riêng khi tới lượt TTS, nghe khựng/bằng bằng —
    xem thảo luận thiết kế), yêu cầu viết LIỀN MẠCH thành MỘT đoạn văn duy nhất cho CẢ LÔ, giữ nguyên dấu
    câu tự nhiên để TTS tự điều phối ngữ điệu/ngắt nghỉ. Trả về CÙNG SCHEMA JSON như `build_batch_prompt`
    (mảng có đúng 1 phần tử) — nhờ vậy toàn bộ phần sau của pipeline (parse, validate_and_fix, cache, TTS,
    time-fit, ghép, ducking, checkpoint) dùng lại NGUYÊN VẸN không cần sửa gì: một "ScriptSegment" dài 16–20s
    chỉ là một segment bình thường có duration_sec lớn hơn, mọi cơ chế hiện có vẫn áp dụng đúng."""
    wps = cfg.words_per_sec
    duration = t1 - t0
    max_words = max(1, int(duration * wps))
    example = [{"id": 1, "start_time": format_timestamp(t0), "end_time": format_timestamp(t1),
                "duration_sec": round(duration, 2), "text": "A single flowing, natural paragraph with proper punctuation...",
                "tone": "hao hung", "certainty": "observed_fact"}]
    frames = "\n".join(f"  Image {i}: {format_timestamp(s.timestamp_sec)}" for i, s in enumerate(batch, start=1))
    prompt = (
        f"You are a professional voice-over scriptwriter. I'm attaching {len(batch)} images extracted "
        f"CONSECUTIVELY from a SILENT video (total duration {format_timestamp(video_duration)}). The top-left "
        "corner of each image has a timestamp label HH:MM:SS.mmm — that's the image's position in the video.\n"
        f"Order of attached images:\n{frames}\n\n"
        f"TASK: write A SINGLE CONTINUOUS FLOWING PARAGRAPH in {cfg.language_name}, style: {cfg.style}, fully "
        f"describing what unfolds from {format_timestamp(t0)} to {format_timestamp(t1)}. Compare consecutive "
        "images to understand the ACTION taking place, in the correct chronological order."
        f"{_narrative_instructions(cfg, is_opening_batch=is_opening_batch, story_hook=story_hook, chunk_outline=chunk_outline)}\n\n"
        "IMPORTANT ABOUT HOW TO WRITE THIS (very different from writing separate standalone sentences):\n"
        "- Do NOT break it into multiple sentences each isolated to one single moment — write it as ONE "
        "FLOWING narration, ideas connected naturally with commas, periods, ellipses... so it rises and falls "
        "naturally when read aloud.\n"
        "- Keep FULL, natural punctuation (, . ... ! ?) — this is the signal the reader uses to pause in the "
        "right places automatically. ABSOLUTELY DO NOT write it as a string of short, disconnected, "
        "list-like sentences.\n\n"
        "MANDATORY RULES:\n"
        "1. Return ONLY ONE ```json ... ``` code block containing a JSON array with EXACTLY 1 ELEMENT in this "
        "exact shape (no preamble):\n"
        f"```json\n{json.dumps(example, ensure_ascii=False, indent=2)}\n```\n"
        f"2. start_time MUST be exactly {format_timestamp(t0)}, end_time MUST be exactly {format_timestamp(t1)} "
        "— DO NOT change them.\n"
        f"3. Speaking rate ≈ {wps:g} words/sec (word count = number of whitespace-separated tokens — in "
        f"Vietnamese that means each syllable counts as one word, not each compound word). The TOTAL word "
        f"count of `text` MUST be ≤ {max_words} words (= {duration:.1f}s × {wps:g} words/sec). Shorter is "
        "better than too long.\n"
        "4. Stay grounded in the real footage, don't invent details; if the whole batch is static/has nothing "
        "worth narrating, write something brief rather than inventing content.\n"
        "5. Natural, expressive spoken language; no emoji, markdown symbols, brackets, hard-to-read "
        "abbreviations; spell out numbers as words when needed.\n"
        '6. tone is ONE shared value for the whole paragraph, must be exactly one of: "vui ve", "hao hung", '
        '"tram am", "trung tinh" (these are fixed internal Vietnamese labels — keep them exactly as written, '
        "do not translate them).\n"
        "7. id = 1 (ONLY one element in the array).\n"
        f"8. {_CERTAINTY_RULE} (certainty is ONE shared value for the whole paragraph)"
    )
    if previous:
        prompt += "\n\nThe narration paragraph IMMEDIATELY BEFORE this one (for natural continuity, do NOT " \
            "repeat its ideas/wording):\n" + previous[-1].text
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
                         story_hook: str | None = None, chunk_outline: str | None = None) -> str:
    wps = cfg.words_per_sec
    example = [{"id": 1, "start_time": "00:00:01.000", "end_time": "00:00:05.500", "duration_sec": 4.5,
                "text": "Narration matching the scene...", "tone": "hao hung", "certainty": "observed_fact"}]
    prompt = (
        f"Here is a YouTube video link: {youtube_url}\n"
        f"This video has NO dialogue/narration (silent/gameplay/B-roll...), total duration "
        f"{format_timestamp(video_duration)}. WATCH closely the segment from {format_timestamp(t0)} to "
        f"{format_timestamp(t1)} of this video (ignore other parts) — pay attention to both the VISUALS and "
        "how the action unfolds over time.\n\n"
        f"TASK: write narration in {cfg.language_name}, style: {cfg.style}, for EXACTLY the "
        f"[{format_timestamp(t0)}, {format_timestamp(t1)}] segment stated above, grounded in the real action/"
        "content taking place.\n"
        f"{_narrative_instructions(cfg, is_opening_batch=is_opening_batch, story_hook=story_hook, chunk_outline=chunk_outline)}\n\n"
        "MANDATORY RULES:\n"
        "1. Return ONLY ONE ```json ... ``` code block containing a JSON array in this exact shape (no preamble/explanation):\n"
        f"```json\n{json.dumps(example, ensure_ascii=False, indent=2)}\n```\n"
        "2. start_time/end_time in HH:MM:SS.mmm format (the video's REAL timestamps, not relative to this "
        "clip); duration_sec = end_time − start_time.\n"
        f"3. Speaking rate ≈ {wps:g} words/sec (word count = number of whitespace-separated tokens — in "
        f"Vietnamese that means each syllable counts as one word, not each compound word). The word count of "
        f"`text` MUST be ≤ duration_sec × {wps:g} (e.g. a 4-second segment gets at most {int(4 * wps)} words). "
        "Shorter is better than too long.\n"
        f"4. ONLY use timestamps within [{format_timestamp(t0)}, {format_timestamp(t1)}]; segments must be "
        f"sequential, NOT overlapping, with ≥ 0.2s gap between two segments, each segment "
        f"{cfg.min_segment_sec:g}–10 seconds long.\n"
        "5. Stay grounded in the video's REAL content, don't invent details; skip a segment if the footage is "
        "static/has nothing worth narrating.\n"
        "6. Natural, expressive, flowing spoken language; no emoji, symbols, brackets, hard-to-read "
        "abbreviations; spell out numbers as words when needed.\n"
        '7. tone must be exactly one of: "vui ve", "hao hung", "tram am", "trung tinh" (these are fixed '
        "internal Vietnamese labels — keep them exactly as written, do not translate them).\n"
        "8. id numbered sequentially starting from 1.\n"
        f"9. {_CERTAINTY_RULE}\n"
        "10. If you CANNOT watch this video's content from this link (private/removed/inaccessible video), "
        'return EXACTLY: ```json\n[]\n```\n and do not guess or invent content.'
    )
    if previous:
        prompt += "\n\nThe narration lines immediately before this (for continuity, do NOT repeat them):\n" + \
            "\n".join(f"- {s.text}" for s in previous[-3:])
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


def plan_batches_by_scene(samples: list[FrameSample], min_batch_sec: float, max_per_batch: int
                         ) -> list[list[FrameSample]]:
    """Gộp frame thành lô THEO ĐÚNG RANH GIỚI CẢNH THẬT (dùng khi `cfg.extract_mode == "scene"`), thay vì
    cắt cứng theo số lượng như `plan_batches()` — mỗi `FrameSample` đã mang sẵn `window_start`/`window_end`
    của đúng cảnh nó thuộc về (từ `video_processor.build_time_windows()`); hàm này chỉ cần GOM LẠI theo
    đúng thông tin đó, không cần dò lại cảnh cắt lần nữa.

    Vì sao cần: trước đây dù đã bật `extract_mode="scene"` để lấy mẫu đúng theo cảnh, bước GHÉP LÔ GỬI
    GEMINI vẫn cắt theo số lượng cố định — một cảnh có thể bị xẻ làm đôi giữa 2 lô, hoặc nhiều cảnh ngắn bị
    trộn lẫn tuỳ ý, khiến mỗi lô không còn tương ứng với MỘT đơn vị nội dung thật nào — đặc biệt ảnh hưởng
    tới `narration_mode="continuous"` (mỗi lô = 1 đoạn văn): đoạn văn khi đó không khớp với bất kỳ cảnh thật
    nào, mất hết ý nghĩa của việc dùng `extract_mode="scene"`.

    `min_batch_sec`: cảnh THẬT quá ngắn (vd 0.5s) sẽ gộp vào lô kế tiếp — tránh sinh ra lô/đoạn văn quá
    vụn, không đủ nội dung để viết một câu/đoạn có ý nghĩa (đúng tinh thần tài liệu: "một paragraph có thể
    đi qua ranh giới nhiều cảnh ngắn — đây là bình thường").
    `max_per_batch`: giới hạn trên số ảnh/lô (phòng trường hợp 1 cảnh quá dài có nhiều frame mẫu) — cắt
    thêm trong CÙNG MỘT cảnh nếu vượt, KHÔNG trộn sang cảnh khác."""
    if not samples:
        return []
    groups: list[list[FrameSample]] = []
    cur: list[FrameSample] = [samples[0]]
    for s in samples[1:]:
        same_window = (s.window_start, s.window_end) == (cur[-1].window_start, cur[-1].window_end)
        if same_window and len(cur) < max_per_batch:
            cur.append(s)
        else:
            groups.append(cur)
            cur = [s]
    groups.append(cur)

    # gộp các nhóm quá ngắn vào nhóm KẾ TIẾP (không gộp ngược về trước, giữ đúng thứ tự thời gian)
    merged: list[list[FrameSample]] = []
    pending: list[FrameSample] = []
    for g in groups:
        pending += g
        duration = pending[-1].window_end - pending[0].window_start
        if duration >= min_batch_sec or g is groups[-1]:
            merged.append(pending)
            pending = []
    if pending:        # phần dư hiếm gặp (vd nhóm cuối vẫn ngắn) — gộp vào lô cuối cùng đã có
        if merged:
            merged[-1] = merged[-1] + pending
        else:
            merged.append(pending)
    return merged


def _fingerprint(cfg: Config, media: MediaInfo) -> dict[str, Any]:
    return {"video": cfg.video_path.name, "size": cfg.video_path.stat().st_size, "duration": round(media.duration_sec, 2),
            "mode": cfg.extract_mode, "interval": cfg.frame_interval_sec, "per_prompt": cfg.frames_per_prompt,
            "language": cfg.language, "style": cfg.style, "wps": cfg.words_per_sec}


def build_chunk_outline_prompt(cfg: Config, sparse_timestamps: list[float], t0: float, t1: float,
                               video_duration: float) -> str:
    """Prompt lập DÀN Ý TỔNG THỂ cho cả chunk (mở đầu/phát triển/cao trào/kết) — phiên bản NHẸ của "3-pass
    Gemini" (hiểu hình ảnh → lập dàn ý → viết lời): thay vì tách thành 2 lượt gọi RIÊNG mỗi LÔ NHỎ (tốn gấp
    ~3 lần số lượt gọi Gemini cho cả video), chỉ thêm ĐÚNG 1 lượt gọi cho CẢ CHUNK LỚN, dùng frame lấy mẫu
    THƯA (không phải mọi frame của mọi lô) — rẻ hơn nhiều, vẫn cho Gemini thấy bối cảnh tổng thể trước khi
    viết từng câu ở các lượt sau."""
    stamps = "\n".join(f"  Image {i}: {format_timestamp(t)}" for i, t in enumerate(sparse_timestamps, start=1))
    example = [{"outline": "This is the opening, introducing the setting... The most notable point is... The emotional arc should..."}]
    return (
        f"You are a scriptwriter. I'm attaching {len(sparse_timestamps)} SPARSELY sampled images, spread "
        f"evenly from {format_timestamp(t0)} to {format_timestamp(t1)} of a video (total duration "
        f"{format_timestamp(video_duration)}) — ONLY so you can grasp the overall context, no need to "
        "describe details.\n"
        f"Order of images:\n{stamps}\n\n"
        "TASK: write a BRIEF OUTLINE (3-5 sentences) for this video segment, stating: whether this is the "
        "OPENING/DEVELOPMENT/CLIMAX/ENDING of the overall story (if identifiable), what's most worth noting, "
        "and which direction the emotional arc should take. This is ONLY an outline to GUIDE the storytelling "
        "in the detailed narration-writing step that follows — do NOT write actual narration, do NOT invent "
        "details absent from the images. If the images aren't clear enough to determine the structure, just "
        "give a brief summary of the general content.\n\n"
        "Return ONLY ONE ```json ... ``` code block containing a JSON array with EXACTLY 1 element in this "
        "shape (no preamble):\n"
        f"```json\n{json.dumps(example, ensure_ascii=False, indent=2)}\n```"
    )


def plan_chunk_outline(driver: Any, samples: list[FrameSample], cfg: Config, media: MediaInfo,
                       chunk_tag: str) -> str | None:
    """Gọi Gemini ĐÚNG 1 LẦN cho cả chunk, lấy dàn ý tổng thể — xem `build_chunk_outline_prompt`. Trả về
    `None` nếu `cfg.enable_story_plan=False` (mặc định — giữ nguyên tốc độ như trước khi có tính năng này),
    hoặc nếu có lỗi (không chặn cả pipeline chỉ vì bước LÀM GIÀU THÊM này thất bại — viết lời vẫn chạy bình
    thường mà không có dàn ý, giống hệt trước đây)."""
    if not cfg.enable_story_plan or not samples:
        return None
    t0, t1 = samples[0].window_start, samples[-1].window_end
    video_duration = media.duration_sec
    cache = cfg.batches_dir / f"{chunk_tag}_outline.json"
    fp = {"range": [round(t0, 3), round(t1, 3)], "style": cfg.style, "narrative_style": cfg.narrative_style,
         "creativity_level": cfg.creativity_level}
    if not cfg.force and cache.is_file():
        try:
            data = read_json(cache)
            if data.get("fingerprint") == fp:
                log.info("Dàn ý chunk (%s → %s): dùng lại kết quả đã lưu.", format_timestamp(t0), format_timestamp(t1))
                return data.get("outline") or None
        except (PipelineError, KeyError, TypeError):
            pass

    n_sparse = min(len(samples), 10)
    step = max(1, len(samples) // n_sparse)
    sparse = samples[::step][:n_sparse]
    prompt = build_chunk_outline_prompt(cfg, [s.timestamp_sec for s in sparse], t0, t1, video_duration)
    try:
        log.info("Đang lập dàn ý tổng thể cho chunk (%s → %s, %d ảnh thưa)...", format_timestamp(t0), format_timestamp(t1), len(sparse))
        raw = driver.ask_json(prompt, [s.path for s in sparse], label=f"{chunk_tag}-outline")
        outline = str(raw[0].get("outline", "")).strip() if raw and isinstance(raw[0], dict) else ""
        cfg.batches_dir.mkdir(parents=True, exist_ok=True)
        write_json(cache, {"fingerprint": fp, "outline": outline})
        if outline:
            log.info("Dàn ý: %s", outline[:200] + ("…" if len(outline) > 200 else ""))
        return outline or None
    except Exception as e:  # noqa: BLE001 — bước LÀM GIÀU THÊM, lỗi ở đây KHÔNG được chặn cả pipeline
        log.warning("Lập dàn ý chunk thất bại (%s) — bỏ qua, viết lời tiếp tục bình thường không có dàn ý.", e)
        return None


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
    if cfg.extract_mode == "scene":
        batches = plan_batches_by_scene(samples, cfg.min_segment_sec, cfg.frames_per_prompt)
    else:
        batches = plan_batches(samples, cfg.frames_per_prompt)
    fp = _fingerprint(cfg, media)
    cfg.batches_dir.mkdir(parents=True, exist_ok=True)
    if cfg.force:
        for f in cfg.batches_dir.glob(f"{batch_tag}_*.json"):
            f.unlink(missing_ok=True)
    segments: list[ScriptSegment] = []
    video_duration = context_duration_sec if context_duration_sec is not None else media.duration_sec
    chunk_outline = plan_chunk_outline(driver, samples, cfg, media, batch_tag)

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
            build_prompt = build_continuous_batch_prompt if cfg.narration_mode == "continuous" else build_batch_prompt
            prompt = build_prompt(cfg, batch, t0, t1, video_duration, segments,
                                  is_opening_batch=opening, story_hook=story_hook, chunk_outline=chunk_outline)
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
    check_certainty(fixed, cfg)
    return fixed


def check_certainty(segments: list[ScriptSegment], cfg: Config) -> dict[str, int]:
    """Đếm phân bố nhãn `certainty` của toàn bộ kịch bản — chỉ BÁO CÁO (log), KHÔNG tự động sửa nội dung
    (tự động viết lại câu "có vẻ đáng ngờ" rủi ro cao hơn lợi ích — có thể sửa nhầm câu vốn đã đúng). Cảnh
    báo khi tỷ lệ "creative_framing" cao bất thường so với mức sáng tạo người dùng đã chọn THẤP — dấu hiệu
    Gemini không tuân thủ đúng yêu cầu, người dùng nên tự xem lại `script.json`."""
    counts = {level: 0 for level in CERTAINTY_LEVELS}
    for s in segments:
        counts[s.certainty] = counts.get(s.certainty, 0) + 1
    total = len(segments)
    if total == 0:
        return counts
    creative_ratio = counts.get("creative_framing", 0) / total
    if cfg.creativity_level <= 0.35 and creative_ratio > 0.2:
        log.warning("Mức sáng tạo THẤP (%.2f) nhưng %.0f%% đoạn (%d/%d) được gắn nhãn 'creative_framing' — "
                   "Gemini có thể chưa tuân thủ đúng yêu cầu, nên xem lại script.json.",
                   cfg.creativity_level, creative_ratio * 100, counts["creative_framing"], total)
    log.info("Phân bố độ tin cậy nội dung: %s", ", ".join(f"{k}={v}" for k, v in counts.items() if v) or "(rỗng)")
    return counts


def _looks_missing_diacritics(text: str, language: str) -> bool:
    """Heuristic: chữ tiếng Việt có dấu (thanh + nguyên âm) chiếm tỷ lệ đáng kể trong văn bản tiếng Việt bình
    thường — nếu một câu chỉ toàn chữ cái ASCII trơn (không "à/á/ả/ã/ạ", "ă/â", "ê", "ô/ơ", "ư", "đ"...) thì
    gần như chắc chắn đã BỊ MẤT DẤU (không phải văn bản tiếng Việt hợp lệ), dù ngữ pháp/từ vựng vẫn đúng.
    Chỉ áp dụng khi `language == "vi"` — ngôn ngữ khác không có khái niệm "dấu" này."""
    if language != "vi" or not text:
        return False
    letters = [c for c in text.lower() if c.isalpha()]
    if len(letters) < 6:            # câu quá ngắn, không đủ tin cậy để kết luận
        return False
    co_dau = sum(1 for c in letters if c in _VI_DIACRITIC_CHARS)
    return co_dau / len(letters) < 0.08     # hiệu chỉnh từ số liệu thật (câu tiếng Việt bình thường, kể cả
    # câu rất ngắn, luôn ≥ ~13% chữ mang dấu; câu mất dấu HOÀN TOÀN = 0%). LƯU Ý: câu mất dấu NỬA CHỪNG (chỉ
    # một phần câu bị mất, phần còn lại vẫn có dấu — từng gặp thật) có thể có tỷ lệ nằm ngay sát vùng bình
    # thường (đo được ~14%), heuristic đơn giản này KHÔNG tách bạch được hoàn toàn trường hợp đó — chỉ chắc
    # chắn bắt được trường hợp phổ biến hơn nhiều: mất dấu HOÀN TOÀN cả câu.


_VI_DIACRITIC_CHARS = set("àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ")


def _shorten(driver: Any, too_long: list[ScriptSegment], cfg: Config, round_no: int) -> None:
    payload = [{"id": s.id, "duration_sec": round(s.duration_sec, 2), "max_words": max_words_for(s, cfg), "text": s.text}
               for s in too_long]
    vi_note = (" MANDATORY: write WITH FULL VIETNAMESE TONE AND VOWEL DIACRITICS as normal (e.g. 'giữa đại "
              "ngàn' — ABSOLUTELY DO NOT drop the diacritics, do NOT write it unaccented like 'giua dai "
              "ngan').") if cfg.language == "vi" else ""
    prompt = (f"You are a voice-over editor. Shorten EACH narration line ({cfg.language_name}) below so its "
              "word count ≤ max_words, keeping the main idea, natural spoken language, same language, no "
              f"added symbols.{vi_note}\n"
              'Return ONLY ONE ```json ... ``` code block containing [{"id": <id>, "text": "<shortened line>"}] '
              "matching exactly these ids:\n"
              f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```")
    raw = driver.ask_json(prompt, [], label=f"shorten-{round_no}")
    by_id = {s.id: s for s in too_long}
    for item in raw:
        try:
            seg = by_id.get(int(item.get("id")))
        except (TypeError, ValueError, AttributeError):
            continue
        new_text = clean_text(item.get("text"))
        if not (seg and new_text and count_words(new_text) < count_words(seg.text)):
            continue
        if _looks_missing_diacritics(new_text, cfg.language):
            # Bản rút gọn bị THIẾU DẤU — KHÔNG dùng (thà câu dài, sau này bị ép tốc độ đọc nhanh hơn, còn hơn
            # đọc sai hẳn tiếng Việt). Giữ nguyên `seg.text` cũ, để lần sau (vòng rút gọn kế, hoặc bước cắt
            # cứng `_truncate_words` cuối cùng) xử lý tiếp.
            log.warning("Đoạn %d: bản rút gọn từ Gemini bị THIẾU DẤU tiếng Việt (%r) → bỏ qua, giữ câu dài.",
                       seg.id, new_text[:60])
            continue
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
