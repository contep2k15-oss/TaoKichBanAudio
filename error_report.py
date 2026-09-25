"""error_report.py — Sinh báo cáo lỗi ĐẦY ĐỦ, DÁN THẲNG VÀO CHAT ĐƯỢC NGAY, khi pipeline gặp sự cố.

Mục đích: khi có lỗi (hoặc chỉ đơn giản "không biết sao lại thế"), người dùng không cần tự đi tìm log,
đọc traceback, hay nhớ đã bật tuỳ chọn gì — chỉ cần copy đúng MỘT khối văn bản và dán cho Claude. Dùng ở
cả GUI (app_streamlit.py, hiện trong khung có nút copy sẵn của Streamlit) và CLI (main.py, in ra + lưu file
để tìm lại được ngay cả khi đã đóng cửa sổ terminal).
"""
from __future__ import annotations

import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Config

REPORT_HEADER = "BÁO CÁO LỖI — Silent Video Voiceover Creator"


def _cfg_summary_lines(cfg: "Config") -> list[str]:
    """Chỉ liệt kê những trường THỰC SỰ hữu ích để chẩn đoán — không dump toàn bộ Config (quá dài, có thể
    lẫn API key). Không bao giờ in api_key/elevenlabs_api_key dù có giá trị hay không."""
    lines = [
        f"  video_path: {cfg.video_path}",
        f"  youtube_url: {getattr(cfg, 'youtube_url', None)!r}",
        f"  engine: {cfg.engine} (browser_channel={getattr(cfg, 'browser_channel', '?')})",
        f"  tts_engine: {cfg.tts_engine} | language: {cfg.language} | voice: {cfg.voice!r}",
        f"  narrator_pov: {cfg.narrator_pov!r} | narrative_style: {cfg.narrative_style}",
        f"  highlight_mode: {cfg.highlight_mode} (target_ratio={cfg.highlight_target_ratio}, "
        f"frame_interval={cfg.highlight_frame_interval_sec})",
        f"  chunk_target_sec: {cfg.chunk_target_sec} | min_chunk_sec: {cfg.min_chunk_sec}",
        f"  min_pause_sec/max_pause_sec: {cfg.min_pause_sec}/{cfg.max_pause_sec}",
        f"  max_speedup: {cfg.max_speedup} | allow_video_retime: {cfg.allow_video_retime}",
        f"  force: {cfg.force} | stop_after: {cfg.stop_after}",
    ]
    return lines


def _log_tail(log_path: Path | None, n: int) -> list[str]:
    if not log_path or not log_path.is_file():
        return ["  (chưa có file log, hoặc lỗi xảy ra trước khi log được tạo)"]
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        tail = text.splitlines()[-n:]
        return tail if tail else ["  (file log rỗng)"]
    except Exception as e:  # noqa: BLE001 — bản thân việc đọc log để BÁO CÁO LỖI không được phép tự crash thêm
        return [f"  (không đọc được log: {e})"]


def _package_versions() -> list[str]:
    lines = []
    for pkg in ("streamlit", "pywebview", "playwright", "pydub", "edge_tts", "cv2"):
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", None)
            if ver is None and pkg == "cv2":
                ver = mod.__version__
            lines.append(f"  {pkg}: {ver or '(không rõ phiên bản)'}")
        except Exception:  # noqa: BLE001
            lines.append(f"  {pkg}: (chưa cài / không import được)")
    return lines


def build_error_report(exc: BaseException | None = None, cfg: "Config | None" = None, *,
                       log_tail_lines: int = 60, context: str | None = None,
                       extra: dict[str, object] | None = None) -> str:
    """`exc=None`: báo cáo CHẨN ĐOÁN chủ động (chưa có lỗi, người dùng chỉ thấy "chạy lâu/kỳ lạ") — vẫn kèm
    đủ cấu hình + log gần nhất, chỉ khác không có traceback. `context`: một câu mô tả ngắn tình huống lúc
    gọi (vd "đang ở Bước 3 — Tạo giọng đọc"), giúp Claude định vị nhanh hơn khi đọc báo cáo."""
    now = datetime.now().astimezone()
    lines = ["```", f"{REPORT_HEADER} — {now.strftime('%Y-%m-%d %H:%M:%S %Z')}", "=" * 60]
    if context:
        lines += [f"Bối cảnh: {context}", ""]

    if exc is not None:
        lines += [f"Loại lỗi: {type(exc).__name__}", f"Thông điệp: {exc}", "", "Traceback đầy đủ:",
                  "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip(), ""]
    else:
        lines += ["(Không có lỗi/exception nào được ghi nhận tại thời điểm tạo báo cáo này — đây là báo "
                  "cáo chẩn đoán chủ động.)", ""]

    lines += ["Môi trường:", f"  Python: {sys.version.split()[0]}", f"  Hệ điều hành: {platform.platform()}",
             f"  Chạy dạng: {'đóng gói (.exe)' if getattr(sys, 'frozen', False) else 'chạy từ source'}",
             *_package_versions(), ""]

    if cfg is not None:
        lines += ["Cấu hình đang dùng:", *_cfg_summary_lines(cfg), ""]
        lines += [f"{log_tail_lines} dòng log gần nhất ({getattr(cfg, 'log_path', None)}):",
                 *_log_tail(getattr(cfg, "log_path", None), log_tail_lines)]

    if extra:
        lines += ["", "Thông tin bổ sung:"] + [f"  {k}: {v}" for k, v in extra.items()]

    lines.append("```")
    return "\n".join(lines)


def save_error_report(report: str, cfg: "Config | None") -> Path | None:
    """Lưu báo cáo ra file (ngoài việc in/hiện trên màn hình) — để tìm lại được ngay cả khi đã đóng cửa sổ
    terminal/trình duyệt. Trả về đường dẫn đã lưu, hoặc None nếu không lưu được (không được phép crash)."""
    try:
        base = cfg.workdir if (cfg is not None and getattr(cfg, "workdir", None)) else Path.cwd()
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"error_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path.write_text(report, encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001
        return None
