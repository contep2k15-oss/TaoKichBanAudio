"""Tải video từ YouTube về máy (dùng `yt-dlp`) — CHỈ cần cho bước TTS/ghép/xuất video cuối cùng (FFmpeg
cần file thật để mux). Bước VIẾT KỊCH BẢN không cần file này — xem `script_generator.generate_script_from_youtube`,
nơi link YouTube được gửi THẲNG cho Gemini Web, không qua bước tải/trích frame cục bộ nào.

LƯU Ý PHÁP LÝ: tải video từ YouTube thường không đúng Điều khoản dịch vụ của YouTube nếu video không thuộc
quyền của bạn hoặc bạn không được phép tải xuống — hãy tự đảm bảo có quyền sử dụng trước khi dùng tính năng
này với video của người khác. Tương tự lưu ý về việc tự động hoá giao diện Gemini Web đã có ở nơi khác.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from config import Config
from utils import MediaToolError, PipelineError, read_json, write_json

log = logging.getLogger("svvc.youtube")

_URL_RE = re.compile(r"^https?://(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w-]+", re.IGNORECASE)


def validate_youtube_url(url: str) -> str:
    """Kiểm tra sơ bộ đây có phải link YouTube hợp lệ không (chỉ kiểm tra hình thức, KHÔNG gọi mạng).
    Trả về URL đã dọn khoảng trắng, hoặc ném PipelineError nếu rõ ràng sai định dạng."""
    url = (url or "").strip()
    if not url:
        raise PipelineError("Chưa nhập link YouTube.")
    if not _URL_RE.match(url):
        raise PipelineError(f"'{url}' không giống link YouTube hợp lệ (cần dạng youtube.com/watch?v=... "
                            "hoặc youtu.be/...).")
    return url


def probe_youtube(url: str) -> dict[str, Any]:
    """Lấy thông tin video (thời lượng, tiêu đề...) mà KHÔNG tải nội dung — dùng để báo lỗi sớm (video
    riêng tư/không tồn tại/bị chặn khu vực...) trước khi tốn công tải cả file."""
    try:
        import yt_dlp
    except ImportError as e:
        raise PipelineError("Chưa cài yt-dlp: pip install yt-dlp") from e
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001 — yt_dlp ném nhiều loại lỗi khác nhau (DownloadError, ExtractorError...)
        raise PipelineError(f"Không lấy được thông tin video YouTube: {e}") from e
    if info.get("is_live"):
        raise PipelineError("Video đang LIVE (phát trực tiếp) — chưa hỗ trợ, hãy đợi video kết thúc.")
    duration = info.get("duration")
    if not duration or duration <= 0:
        raise PipelineError("Không xác định được thời lượng video (có thể là video riêng tư/bị hạn chế).")
    return {"title": info.get("title") or url, "duration": float(duration), "id": info.get("id") or url}


def _fingerprint(url: str, video_id: str) -> dict[str, Any]:
    return {"url": url, "video_id": video_id}


def get_or_download(cfg: Config, url: str) -> Path:
    """Tải video YouTube về `cfg.workdir/youtube/`, có cache theo URL — chạy lại với đúng link sẽ dùng lại
    file đã tải, không tải lại. Trả về đường dẫn file cục bộ (định dạng mp4, video H.264 + audio AAC để
    FFmpeg ở các bước sau dùng lại dễ dàng, không cần chuyển mã)."""
    url = validate_youtube_url(url)
    info = probe_youtube(url)
    fp = _fingerprint(url, info["id"])
    yt_dir = cfg.workdir / "youtube"
    yt_dir.mkdir(parents=True, exist_ok=True)
    fp_hash = hashlib.sha1(json.dumps(fp, sort_keys=True).encode()).hexdigest()[:12]
    meta_path, video_path = yt_dir / f"{fp_hash}.json", yt_dir / f"{fp_hash}.mp4"

    if meta_path.is_file() and video_path.is_file():
        try:
            meta = read_json(meta_path)
            if meta.get("fingerprint") == fp:
                log.info("Đã có sẵn video YouTube đã tải từ trước → dùng lại: %s (%s, %.0fs).",
                         video_path.name, info["title"], info["duration"])
                return video_path
        except PipelineError:
            pass

    log.info("Đang tải video YouTube: %s (%.0fs)...", info["title"], info["duration"])
    try:
        import yt_dlp
    except ImportError as e:
        raise PipelineError("Chưa cài yt-dlp: pip install yt-dlp") from e

    tmp_template = str(yt_dir / f"{fp_hash}.download.%(ext)s")
    ydl_opts = {
        "quiet": True, "no_warnings": True, "outtmpl": tmp_template,
        # ưu tiên video H.264 + audio AAC đóng gói sẵn trong .mp4 để không phải chuyển mã lại ở bước sau
        "format": "best[ext=mp4][vcodec^=avc1][acodec^=mp4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"Tải video YouTube thất bại: {e}") from e

    downloaded = sorted(yt_dir.glob(f"{fp_hash}.download.*"))
    if not downloaded:
        raise MediaToolError("yt-dlp báo tải xong nhưng không tìm thấy file kết quả.")
    downloaded[0].replace(video_path)
    for leftover in yt_dir.glob(f"{fp_hash}.download.*"):
        leftover.unlink(missing_ok=True)

    write_json(meta_path, {"fingerprint": fp, "title": info["title"], "duration": info["duration"]})
    log.info("Đã tải xong: %s → %s", info["title"], video_path.name)
    return video_path
