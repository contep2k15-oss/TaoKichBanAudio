"""Tiện ích dùng chung: exception, logging, timestamp, gọi FFmpeg/ffprobe, đọc metadata media."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

log = logging.getLogger("svvc.utils")


# ════════════════════════════════════════════════════════════
# Exceptions
# ════════════════════════════════════════════════════════════
class PipelineError(Exception):
    """Lỗi gốc của pipeline — main.py bắt lỗi này và in thông báo thân thiện."""


class MediaToolError(PipelineError):
    """FFmpeg / ffprobe / OpenCV lỗi hoặc không tìm thấy."""


class GeminiError(PipelineError):
    """Gemini API lỗi (mạng, quota, JSON hỏng sau khi đã retry...)."""


class JSONParseError(GeminiError):
    """Không parse được JSON từ phản hồi của Gemini."""


# ── Gemini Web (Playwright) ─────────────────────────────────
class GeminiWebError(GeminiError):
    """Lỗi chung khi điều khiển giao diện Gemini Web."""


class WebAuthError(GeminiWebError):
    """Chưa đăng nhập / phiên hết hạn."""


class WebBlockedError(GeminiWebError):
    """Bị chặn tạm thời: captcha, 'unusual traffic', hết hạn mức, quá nhiều yêu cầu."""


class WebDOMError(GeminiWebError):
    """Cấu trúc trang thay đổi (selector lỗi thời) hoặc nhận HTML thay vì câu trả lời."""


class WebNetworkError(GeminiWebError):
    """Mạng nghẽn / trang lỗi / timeout."""


class WebResponseFormatError(GeminiWebError):
    """Gemini trả lời nhưng không phải JSON hợp lệ (văn xuôi, bị từ chối, bị cắt cụt)."""


class ScriptError(PipelineError):
    """Kịch bản không hợp lệ."""


class TTSError(PipelineError):
    """Edge-TTS lỗi."""


# ════════════════════════════════════════════════════════════
# Logging
# ════════════════════════════════════════════════════════════
def setup_logging(log_file: Path | None = None, verbose: bool = False) -> None:
    """Console (INFO/DEBUG) + file (luôn DEBUG). An toàn khi gọi nhiều lần (Streamlit re-run)."""
    root = logging.getLogger("svvc")
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.propagate = False

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-11s | %(message)s", "%H:%M:%S")

    # Console UTF-8 để không lỗi tiếng Việt trên Windows (cp1252)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-11s | %(message)s"))
        root.addHandler(fh)

    for noisy in ("httpx", "httpcore", "urllib3", "PIL", "asyncio", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def banner(step: int, total: int, title: str) -> None:
    logging.getLogger("svvc").info("═" * 8 + f" BƯỚC {step}/{total}: {title} " + "═" * 8)


# ════════════════════════════════════════════════════════════
# Timestamp / text
# ════════════════════════════════════════════════════════════
def parse_timestamp(value: Any) -> float:
    """'00:00:01.500' | '01:05.2' | '12.5' | 12.5 | '00:00:01,500' → giây (float)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s:
        raise ValueError("timestamp rỗng")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(f"timestamp không hợp lệ: {value!r}")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"timestamp không hợp lệ: {value!r}") from None
    seconds = 0.0
    for n in nums:
        seconds = seconds * 60 + n
    if seconds < 0:
        raise ValueError(f"timestamp âm: {value!r}")
    return seconds


def format_timestamp(seconds: float) -> str:
    """giây → 'HH:MM:SS.mmm'."""
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def strip_accents(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def count_words(text: str) -> int:
    """Đếm 'từ' — tiếng Việt tách theo khoảng trắng (mỗi tiếng = 1 từ), khớp với quy ước 3-4 từ/giây."""
    return len(_WORD_RE.findall(text or ""))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise PipelineError(f"Không tìm thấy file: {path}") from None
    except json.JSONDecodeError as e:
        raise PipelineError(f"File JSON hỏng ({path}): {e}") from e


# ════════════════════════════════════════════════════════════
# FFmpeg / ffprobe
# ════════════════════════════════════════════════════════════
_INSTALL_HINT = (
    "Hãy cài FFmpeg và đảm bảo có trong PATH — Windows: `winget install Gyan.FFmpeg`, "
    "macOS: `brew install ffmpeg`, Ubuntu: `sudo apt install ffmpeg`."
)


def check_binaries() -> None:
    missing = [b for b in ("ffmpeg", "ffprobe") if shutil.which(b) is None]
    if missing:
        raise MediaToolError(f"Không tìm thấy: {', '.join(missing)}. {_INSTALL_HINT}")


_console_suppressed = False


def suppress_console_windows() -> None:
    """Gọi MỘT LẦN lúc khởi động app (main.py, desktop_app.py, app_streamlit.py) — vá thẳng vào
    `subprocess.Popen` để MỌI tiến trình con được tạo trong suốt vòng đời app đều tự động mang cờ ẩn
    console trên Windows, KỂ CẢ những lệnh gọi FFmpeg do thư viện bên thứ ba (`pydub`) tự thực hiện ngầm.

    Lý do bắt buộc phải vá ở tầng này thay vì chỉ sửa `run_cmd()`: `pydub` (dùng ở khắp `tts_engine.py`,
    `audio_assembler.py`, `video_muxer.py` để đọc/ghi/đo audio) tự gọi `subprocess.Popen` để chạy FFmpeg
    NGẦM bên trong chính nó — không có cách nào truyền `creationflags` vào qua API công khai của pydub.
    Một lượt xử lý TTS cho video dài có thể kích hoạt HÀNG TRĂM lượt gọi như vậy (mỗi đoạn thuyết minh vài
    lần: đọc mp3, cắt khoảng lặng, xuất wav...); nếu không vá ở đây, mỗi lượt sẽ tự bật một cửa sổ console
    mới trên Windows (vì app chạy dưới dạng .exe đóng gói không có console riêng để kế thừa) — vừa gây
    nháy/tràn cửa sổ đen liên tục, vừa có thể gây lỗi khởi tạo tiến trình con khi bị dồn dập cùng lúc.
    """
    global _console_suppressed
    if sys.platform != "win32" or _console_suppressed:
        return
    _console_suppressed = True
    no_window = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    orig_init = subprocess.Popen.__init__

    def patched_init(self: subprocess.Popen, *args: Any, **kwargs: Any) -> None:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | no_window
        orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched_init  # type: ignore[method-assign]
    log.debug("Đã bật chế độ ẩn cửa sổ console cho mọi tiến trình con (Windows).")


def _subprocess_kwargs() -> dict[str, Any]:
    """Cờ ẩn console cho các lệnh gọi qua `run_cmd()` — dự phòng độc lập, không phụ thuộc việc
    `suppress_console_windows()` đã được gọi hay chưa (vd khi dùng `utils.py` như thư viện độc lập)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


def run_cmd(cmd: list[Any], desc: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Chạy lệnh ngoài KHÔNG qua shell. Mỗi lệnh chỉ có vài đối số → không thể dính giới hạn
    độ dài command-line của Windows (WinError 206)."""
    args = [str(c) for c in cmd]
    log.debug("RUN [%s]: %s", desc, " ".join(args))
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, **_subprocess_kwargs())
    except FileNotFoundError as e:
        raise MediaToolError(f"Không tìm thấy '{args[0]}'. {_INSTALL_HINT}") from e
    except subprocess.TimeoutExpired as e:
        raise MediaToolError(f"{desc}: quá thời gian chờ ({timeout}s)") from e
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise MediaToolError(f"{desc} thất bại (exit {proc.returncode}):\n{tail}")
    return proc


@dataclass
class MediaInfo:
    path: Path
    duration_sec: float
    has_audio: bool
    width: int
    height: int
    fps: float

    @property
    def duration_ms(self) -> int:
        return int(round(self.duration_sec * 1000))


def probe_media(path: Path) -> MediaInfo:
    """Đọc metadata bằng ffprobe (có fallback OpenCV cho duration)."""
    if not path.is_file():
        raise MediaToolError(f"Không tìm thấy file video: {path}")
    proc = run_cmd(["ffprobe", "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", path], "ffprobe")
    try:
        info = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise MediaToolError(f"ffprobe trả về dữ liệu hỏng cho {path.name}") from e

    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaToolError(f"'{path.name}' không có luồng video.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = 0.0
    for raw in (info.get("format", {}).get("duration"), video.get("duration")):
        try:
            duration = float(raw)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    fps = 0.0
    try:
        fps = float(Fraction(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"))
    except (ValueError, ZeroDivisionError):
        pass

    if duration <= 0 or fps <= 0:  # fallback OpenCV
        try:
            import cv2
            cap = cv2.VideoCapture(str(path))
            cv_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
            cap.release()
            fps = fps or cv_fps
            if duration <= 0 and cv_fps > 0:
                duration = frames / cv_fps
        except Exception as e:  # noqa: BLE001
            log.debug("Fallback OpenCV lỗi: %s", e)
    if duration <= 0:
        raise MediaToolError(f"Không xác định được thời lượng của '{path.name}'.")

    return MediaInfo(path=path, duration_sec=duration, has_audio=has_audio,
                     width=int(video.get("width") or 0), height=int(video.get("height") or 0),
                     fps=fps or 25.0)
