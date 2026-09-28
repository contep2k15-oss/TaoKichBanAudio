"""youtube_source.py — Nhập kịch bản từ LINK YOUTUBE, gửi thẳng cho Gemini Web (không trích frame cục bộ).

Cách hoạt động (xem giải thích đầy đủ trong long_video_pipeline.py, nơi 2 việc dưới đây chạy SONG SONG):
  1. `analyze_youtube_video()` — gõ một tin nhắn có chứa NGUYÊN VĂN link YouTube vào khung chat Gemini Web
     (không đính kèm ảnh nào — xem `GeminiWebDriver.ask()`: `images=()` thì bỏ qua hẳn bước đính kèm file),
     yêu cầu Gemini tự xem trực tiếp video tại link đó và viết kịch bản đầy đủ theo ĐÚNG schema JSON đang
     dùng cho mọi kịch bản khác trong dự án — nhờ vậy toàn bộ phần parse/validate phía sau
     (`script_generator._coerce_segment`, `validate_and_fix`) dùng lại được y nguyên, không cần code mới.
  2. `download_youtube_video()` — tải file video thật về máy bằng thư viện `yt-dlp` (gọi trực tiếp qua
     Python API, KHÔNG qua subprocess — để hoạt động đồng nhất cả khi chạy từ source lẫn khi đã đóng gói
     thành .exe, xem giải thích trong hàm). File này vẫn cần thiết cho các bước sau (TTS, ghép audio, mux)
     — Gemini "xem" video từ xa chỉ giúp bỏ được bước trích frame + gọi Gemini nhiều lô cho việc VIẾT KỊCH
     BẢN, không giúp bỏ được việc cần có file video thật để dựng video cuối cùng.

LƯU Ý QUAN TRỌNG (đã trao đổi rõ với người dùng trước khi triển khai — xem lịch sử hội thoại):
  - Cơ chế "Gemini Web thực sự xem được hình ảnh khi dán link YouTube vào khung chat" được xác nhận qua
    KIỂM CHỨNG THỰC TẾ của người dùng, không phải tài liệu chính thức của Google (tài liệu chính thức chỉ
    xác nhận rõ ràng cho API, không nói rõ hành vi khi dán link vào chat). Độ tin cậy/giới hạn (video rất
    dài, mật độ mô tả xuyên suốt...) CHƯA được kiểm chứng đầy đủ — cần người dùng tự thử với video thật.
  - Tải video YouTube về máy (kể cả chỉ để dựng lại làm video có thuyết minh) có thể không đúng Điều khoản
    dịch vụ của YouTube nếu video không thuộc quyền của người dùng — đây là lựa chọn của người dùng, không
    phải giới hạn kỹ thuật.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from config import Config
from script_generator import _narrative_instructions
from utils import PipelineError

log = logging.getLogger("svvc.youtube")

_YOUTUBE_RE = re.compile(r"^https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w-]+")


def is_youtube_url(url: str) -> bool:
    return bool(_YOUTUBE_RE.match(url.strip()))


def local_video_path(workdir: Path, max_height: int) -> Path:
    """Đường dẫn file video đã tải, ĐẶT TÊN THEO ĐỘ CAO TỐI ĐA đã chọn — đổi chất lượng tải thì tự tải lại
    file mới (không dùng nhầm bản cũ khác độ phân giải), đồng thời `build_fingerprint` (dựa vào tên/kích
    thước file) tự tách checkpoint riêng, không lẫn giữa các bản."""
    return workdir / f"youtube_source_{max_height}p.mp4"


def format_selector(max_height: int) -> str:
    """Chuỗi chọn định dạng của yt-dlp: GIỚI HẠN độ cao và ƯU TIÊN codec H.264 (avc1).

    Vì sao không để yt-dlp tự chọn "tốt nhất": với video YouTube 4K, bản mp4 độ phân giải cao nhất thường
    là AV1 — codec giải mã bằng phần mềm rất chậm (đo thực tế ~14 khung/giây ở 4K: quét khoảng lặng video
    19 phút mất 40 phút khi còn giải mã cả hình). Tool này KHÔNG cần 4K để làm việc (chỉ lấy khung nhỏ cho
    Gemini, còn ghép video dùng `-c:v copy` nên độ phân giải gốc giữ nguyên) — H.264 ≤1080p giải mã nhanh
    gấp nhiều lần, file nhẹ hơn nhiều, mà kết quả xuất ra vẫn đủ nét cho hầu hết mục đích."""
    h = int(max_height)
    if h > 1080:
        # Người dùng CHỦ ĐỘNG chọn độ phân giải cao (1440p/2160p): tôn trọng lựa chọn — lấy bản cao nhất ≤ h,
        # không ép H.264 (H.264 hầu như không có ở độ phân giải này nên ép sẽ âm thầm hạ xuống 1080p).
        return f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/b[height<={h}]/b"
    return (f"bv*[height<={h}][vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={h}][ext=mp4]/b[height<={h}]/b")


def build_youtube_prompt(cfg: Config, url: str) -> str:
    wps = cfg.words_per_sec
    example = [{"id": 1, "start_time": "00:00:01.000", "end_time": "00:00:05.500", "duration_sec": 4.5,
                "text": "Lời thuyết minh khớp với phân cảnh...", "tone": "hao hung"}]
    prompt = (
        f"Đây là link video: {url}\n\n"
        f"Bạn là biên kịch lồng tiếng chuyên nghiệp. Hãy XEM TRỰC TIẾP video tại link trên (cả hình ảnh lẫn "
        f"âm thanh nếu có) và viết lời thuyết minh bằng {cfg.language_name}, phong cách: {cfg.style}, PHỦ "
        f"TOÀN BỘ video từ đầu đến cuối. So sánh diễn biến hình ảnh theo thời gian để hiểu HÀNH ĐỘNG đang "
        f"diễn ra ở từng thời điểm."
        f"{_narrative_instructions(cfg, is_opening_batch=True, story_hook=None)}\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "1. Chỉ trả về MỘT khối mã ```json ... ``` duy nhất chứa mảng JSON theo đúng mẫu (không thêm lời dẫn/giải thích):\n"
        f"```json\n{json.dumps(example, ensure_ascii=False, indent=2)}\n```\n"
        "2. start_time/end_time dạng HH:MM:SS.mmm (giờ:phút:giây.mili giây, kể cả khi video ngắn dưới 1 giờ "
        "vẫn viết đủ '00:'); duration_sec = end_time − start_time.\n"
        f"3. Tốc độ đọc ≈ {wps:g} từ/giây (tiếng Việt: mỗi tiếng ngăn cách bằng khoảng trắng là 1 từ). Số từ của "
        f"`text` PHẢI ≤ duration_sec × {wps:g}. Thà ngắn còn hơn quá dài.\n"
        "4. Các đoạn nối tiếp theo đúng thứ tự thời gian thật của video, KHÔNG chồng lấn, chừa ≥ 0.2 giây giữa "
        f"hai đoạn, mỗi đoạn dài {cfg.min_segment_sec:g}–10 giây, PHỦ KÍN từ đầu tới cuối video (đừng dừng giữa "
        "chừng) — nếu video dài, vẫn cố gắng viết đủ cho TOÀN BỘ thời lượng, không chỉ phần đầu.\n"
        "5. Bám sát hình ảnh/âm thanh thật của video, không bịa chi tiết; đoạn tĩnh/không có gì đáng nói thì bỏ qua.\n"
        "6. Văn nói tự nhiên, truyền cảm, liền mạch; không emoji, ký hiệu, ngoặc, viết tắt khó đọc; số viết thành chữ khi cần.\n"
        '7. tone chỉ được là một trong: "vui ve", "hao hung", "tram am", "trung tinh" (không dấu).\n'
        "8. id đánh số tăng dần từ 1."
    )
    return prompt


def analyze_youtube_video(driver: Any, cfg: Config, url: str) -> list[dict]:
    """Gửi MỘT tin nhắn duy nhất chứa link (không đính kèm ảnh) — xem giải thích ở đầu file. Trả về mảng
    JSON thô (chưa qua `_coerce_segment`/`validate_and_fix` — gọi ở nơi dùng, giống hệt cách
    `script_generator.generate_script()` xử lý từng lô ảnh bình thường)."""
    log.info("Gửi link YouTube cho Gemini Web, yêu cầu viết kịch bản cho TOÀN BỘ video: %s", url)
    prompt = build_youtube_prompt(cfg, url)
    raw = driver.ask_json(prompt, [], label="youtube-full")
    log.info("Gemini đã trả về %d đoạn thuyết minh (từ link YouTube, chưa qua kiểm tra/rút gọn).", len(raw))
    return raw


# ════════════════════════════════════════════════════════════
# 2) Tải video thật về máy (cần cho TTS + ghép audio + xuất video cuối — xem giải thích ở đầu file)
# ════════════════════════════════════════════════════════════
def download_youtube_video(url: str, out_path: Path, *, max_height: int = 1080,
                           ffmpeg_location: str | None = None) -> Path:
    """Gọi thư viện `yt-dlp` TRỰC TIẾP qua Python API (không qua subprocess) — để hoạt động đồng nhất cả
    khi chạy từ source lẫn khi đã đóng gói .exe (PyInstaller), nơi không có "trình thông dịch Python" độc
    lập để gọi `python -m yt_dlp` qua subprocess như cách dự án vẫn gọi FFmpeg.

    `out_path` PHẢI có đuôi `.mp4` — ép `merge_output_format=mp4` nên file cuối chắc chắn đúng đuôi này,
    tránh phải dò tìm tên file thật sau khi tải (yt-dlp có thể đổi đuôi tuỳ định dạng gốc nếu không ép)."""
    try:
        import yt_dlp
    except ImportError as e:
        raise PipelineError("Chưa cài yt-dlp: pip install yt-dlp") from e

    out_path.parent.mkdir(parents=True, exist_ok=True)
    opts: dict[str, Any] = {
        "outtmpl": str(out_path), "format": format_selector(max_height),
        "merge_output_format": "mp4", "quiet": True, "no_warnings": True, "noprogress": True,
        "retries": 3, "socket_timeout": 30, "overwrites": True,
    }
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    log.info("Đang tải video từ YouTube (tối đa %dp, ưu tiên H.264): %s", max_height, url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private video" in msg or "unavailable" in msg.lower():
            raise PipelineError(f"Không tải được video (riêng tư/đã gỡ/không khả dụng): {msg.splitlines()[-1]}") from e
        raise PipelineError(f"Tải video YouTube thất bại: {msg.splitlines()[-1]}") from e

    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise PipelineError(f"yt-dlp báo xong nhưng không thấy file kết quả: {out_path}")
    log.info("Đã tải xong video: %s (%.1f MB)", out_path.name, out_path.stat().st_size / 1e6)
    return out_path
