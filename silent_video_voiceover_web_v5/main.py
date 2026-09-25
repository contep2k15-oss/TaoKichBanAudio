#!/usr/bin/env python3
"""Silent Video Voiceover Creator - CLI (Gemini Web/Playwright hoặc Gemini API; Edge-TTS hoặc ElevenLabs).

Hỗ trợ VIDEO DÀI (30-60+ phút): video được tự động chia thành các macro-chunk (xem chunk_planner.py) tại
các điểm an toàn (khoảng lặng/chuyển cảnh), xử lý và checkpoint TỪNG CHUNK độc lập (checkpoint.py) - mất
mạng/dừng giữa chừng, chạy lại đúng lệnh cũ sẽ tự RESUME từ chunk dở dang, không phải làm lại từ đầu. Video
ngắn hơn ngưỡng chia chunk (--chunk-minutes) tự động chỉ có 1 chunk - không cần cờ gì đặc biệt.

    python main.py --login                       # đăng nhập Gemini Web bằng tay 1 lần (lưu profile)
    python main.py --check-web --headed          # kiểm tra phiên + selector giao diện Gemini Web
    python main.py demo.mp4                      # chạy pipeline (mặc định --engine web, --tts-engine edge)
    python main.py demo.mp4 --engine api         # dùng Gemini API (cần GEMINI_API_KEY)
    python main.py long_demo.mp4 --chunk-minutes 5   # video dài, mỗi chunk ~5 phút (mặc định 4 phút)
    python main.py demo.mp4 --stop-after 2       # chỉ tạo kịch bản để duyệt/sửa tay (mọi chunk)
    python main.py demo.mp4 --script-file output/demo/script.json   # dùng kịch bản đã sửa (không cần Gemini)
    python main.py demo.mp4 --tts-engine elevenlabs --voice <voice_id>   # đổi engine TTS
    python main.py --list-voices vi
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from config import DEFAULT_GEMINI_MODEL, DEFAULT_GEMINI_URL, Config
from utils import GeminiWebError, PipelineError, WebAuthError, setup_logging, suppress_console_windows

log = logging.getLogger("svvc.main")


# ════════════════════════════════════════════════════════════
# Gemini Web: mở phiên (tự gợi ý đăng nhập nếu cần) - dùng lại logic trong engine_factory.py
# ════════════════════════════════════════════════════════════
def open_web_driver(cfg: Config, interactive: bool | None = None) -> Any:
    """Trả về GeminiWebDriver (Playwright) ĐÃ đăng nhập, dùng cho các lệnh CLI cần thao tác trực tiếp
    (`--check-web`). Pipeline chính KHÔNG gọi hàm này - nó dùng `engine_factory.open_vision_engine()`,
    vốn dùng chung logic đăng nhập/mở phiên này nhưng trả về qua giao diện BaseVisionEngine."""
    from engine_factory import _open_gemini_web
    return _open_gemini_web(cfg, interactive).driver


# ════════════════════════════════════════════════════════════
# Pipeline chính - uỷ quyền cho long_video_pipeline.run() (xử lý cả video ngắn lẫn video dài, có
# checkpoint/resume, chia chunk động, tách engine - xem long_video_pipeline.py để biết chi tiết luồng)
# ════════════════════════════════════════════════════════════
def run_pipeline(cfg: Config, on_step: Callable[[int, str], None] | None = None,
                 on_chunk_progress: Callable[[int, int], None] | None = None) -> dict[str, Path | None]:
    import long_video_pipeline
    return long_video_pipeline.run(cfg, on_step=on_step, on_chunk_progress=on_chunk_progress)


# ════════════════════════════════════════════════════════════
# Lệnh phụ: --login / --check-web / --list-voices
# ════════════════════════════════════════════════════════════
def cmd_login(cfg: Config) -> int:
    from gemini_web_driver import interactive_login
    return 0 if interactive_login(cfg) else 1


def cmd_check_web(cfg: Config) -> int:
    import cv2
    import numpy as np
    from gemini_web_driver import GeminiWebDriver
    cfg.ensure_dirs()
    img = np.full((180, 320, 3), 90, np.uint8)
    cv2.putText(img, "SVVC TEST", (40, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    test_img = cfg.workdir / "selfcheck.jpg"
    test_img.write_bytes(cv2.imencode(".jpg", img)[1].tobytes())
    with GeminiWebDriver(cfg) as drv:
        rows = drv.selfcheck(test_upload=test_img)
    print("\nKẾT QUẢ KIỂM TRA GEMINI WEB")
    for name, ok, detail in rows:
        print(f"  {'✔' if ok else '✖'} {name:<44} {detail}")
    print(f"\nDữ liệu chẩn đoán (ảnh chụp, HTML): {cfg.web_debug_dir}")
    bad = [r for r in rows if not r[1]]
    if bad:
        print("→ Có hạng mục lỗi: xem web_debug/, rồi cập nhật selector bằng --selectors-file (README, mục 'Khi Gemini đổi giao diện').")
    return 1 if bad else 0


def cmd_list_voices(locale: str) -> int:
    try:
        import edge_tts
        voices = asyncio.run(edge_tts.list_voices())
    except Exception as e:  # noqa: BLE001
        print(f"Không lấy được danh sách giọng (cần Internet): {e}", file=sys.stderr)
        return 1
    rows = [v for v in voices if v["Locale"].lower().startswith(locale.lower())]
    for v in sorted(rows, key=lambda v: v["ShortName"]):
        print(f'{v["ShortName"]:<32} {v["Gender"]:<7} {v["Locale"]}')
    return 0 if rows else 1


# ════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Tự động thuyết minh video (kể cả video dài) - Gemini (Web/API) + "
                                "Edge-TTS/ElevenLabs + pydub + FFmpeg.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("video", nargs="?", type=Path, help="video đầu vào")
    p.add_argument("--youtube-url", metavar="URL", help="THAY cho tham số video: gửi thẳng link YouTube cho "
                   "Gemini Web viết kịch bản (không trích frame cục bộ), video vẫn được tải về máy song "
                   "song để phục vụ ghép/xuất — chỉ dùng được với --engine web")
    p.add_argument("-o", "--output-dir", type=Path, help="thư mục kết quả (mặc định: output/<tên video>)")
    p.add_argument("--login", action="store_true", help="đăng nhập Gemini Web bằng tay một lần rồi thoát")
    p.add_argument("--check-web", action="store_true", help="kiểm tra phiên + selector Gemini Web rồi thoát")
    p.add_argument("--list-voices", metavar="LOCALE", help="liệt kê giọng Edge-TTS (vd: vi) rồi thoát")

    g = p.add_argument_group("engine & Gemini Web")
    g.add_argument("--engine", choices=["web", "api"], default="web", help="web = Playwright điều khiển gemini.google.com; api = google-genai")
    g.add_argument("--user-data-dir", type=Path, help="thư mục profile trình duyệt (mặc định ~/.silent_video_voiceover/gemini_profile)")
    g.add_argument("--browser", choices=["chrome", "msedge", "chromium"], default="chrome")
    g.add_argument("--headed", action="store_true", help="hiện cửa sổ trình duyệt (mặc định chạy headless)")
    g.add_argument("--gemini-url", default=DEFAULT_GEMINI_URL)
    g.add_argument("--web-model", default="Flash|Fast|Nhanh", help="regex tên model cần chọn trên giao diện")
    g.add_argument("--selectors-file", type=Path, help="JSON ghi đè/bổ sung selector khi Gemini đổi giao diện")
    g.add_argument("--response-timeout", type=float, default=240.0, help="giây chờ mỗi câu trả lời")
    g.add_argument("--web-delay", type=float, default=3.0, help="giây nghỉ tối thiểu giữa hai prompt")
    g.add_argument("--api-key", help="(engine api) mặc định $GEMINI_API_KEY")
    g.add_argument("--gemini-model", help=f"(engine api) mặc định $GEMINI_MODEL hoặc {DEFAULT_GEMINI_MODEL}")

    g = p.add_argument_group("ngôn ngữ & giọng đọc (TTS)")
    g.add_argument("--language", default="vi", choices=["vi", "en"])
    g.add_argument("--tts-engine", choices=["edge", "elevenlabs"], default="edge",
                   help="edge = Edge-TTS (miễn phí); elevenlabs = ElevenLabs API (trả phí, cần --elevenlabs-api-key)")
    g.add_argument("--voice", help="Edge-TTS: tên giọng (mặc định vi-VN-HoaiMyNeural/en-US-AriaNeural). "
                   "ElevenLabs: voice_id (lấy qua GET /v1/voices).")
    g.add_argument("--elevenlabs-api-key", help="mặc định $ELEVENLABS_API_KEY")
    g.add_argument("--elevenlabs-model", default="eleven_multilingual_v2")
    g.add_argument("--style", default=Config.style)
    g.add_argument("--narrator-pov", default="", help="Ngôi kể/xưng hô (vd 'Tôi', 'Chúng ta', 'Người quan sát'); "
                   "để trống = để Gemini tự chọn")
    g.add_argument("--narrative-style", choices=["natural", "humorous", "formal", "fantasy_inspiring"], default="natural",
                   help="'fantasy_inspiring' = mở đầu bằng câu chuyện giả tưởng, thỉnh thoảng nhắc lại xuyên suốt video")

    g = p.add_argument_group("trích frame & kịch bản")
    g.add_argument("--mode", choices=["interval", "scene"], default="interval")
    g.add_argument("--interval", type=float, default=2.5, help="giây giữa hai frame/cửa sổ")
    g.add_argument("--scene-threshold", type=float, default=0.35, help="ngưỡng cắt cảnh (khoảng cách histogram 0-1)")
    g.add_argument("--frames-per-prompt", type=int, help="số ảnh mỗi prompt (mặc định 8 với web, 16 với api; web tối đa 10)")
    g.add_argument("--max-frames", type=int, default=400)
    g.add_argument("--wps", type=float, help="số từ/giây khi đọc (mặc định 3.2 vi, 2.5 en)")

    g = p.add_argument_group("video DÀI: phân đoạn động (chunking)")
    g.add_argument("--chunk-minutes", type=float, default=4.0, help="độ dài mục tiêu mỗi macro-chunk (phút)")
    g.add_argument("--chunk-tolerance-sec", type=float, default=45.0, help="dung sai tìm điểm cắt an toàn quanh mốc mục tiêu")
    g.add_argument("--min-chunk-sec", type=float, default=60.0, help="không chunk nào ngắn hơn ngần này (trừ chunk cuối)")
    g.add_argument("--silence-min-sec", type=float, default=0.6, help="khoảng lặng tối thiểu tính là điểm cắt an toàn")
    g.add_argument("--silence-noise-db", type=float, default=-30.0, help="ngưỡng coi là im lặng (dBFS)")
    g.add_argument("--force-chunk", type=int, metavar="INDEX", help="chỉ làm lại (bỏ checkpoint) một chunk cụ thể, theo index (0-based)")

    g = p.add_argument_group("chỉ giữ cảnh hay (highlight) — chạy TRƯỚC mọi thứ khác, đỡ tốn tài nguyên xử lý toàn video")
    g.add_argument("--highlight-mode", action="store_true",
                   help="chỉ giữ lại những cảnh đắt giá nhất (Gemini tự chọn) trước khi làm bất cứ gì khác")
    g.add_argument("--highlight-target-ratio", type=float, default=0.4,
                   help="giữ lại khoảng bao nhiêu %% tổng thời lượng gốc, vd 0.4 = 40%%")
    g.add_argument("--highlight-frame-interval", type=float, default=6.0,
                   help="giây giữa 2 lần lấy mẫu khi quét nhanh tìm highlight (thưa hơn hẳn bình thường)")

    g = p.add_argument_group("TTS & âm thanh")
    g.add_argument("--max-speedup", type=float, default=1.25, help="tăng tốc audio tối đa qua FFmpeg atempo (r = actual/target)")
    g.add_argument("--allow-video-retime", action="store_true",
                   help="LAST-RESORT: nếu audio vẫn dài hơn cả khoảng trống sau khi tăng tốc tối đa, cho phép làm CHẬM "
                   "khung hình video trong đúng cửa sổ đó thay vì cắt cụt lời thoại (mặc định TẮT, tốn thời gian re-encode)")
    g.add_argument("--tts-concurrency", type=int, default=4)
    g.add_argument("--bgm-duck", type=float, default=0.7, help="âm lượng BGM (0-1) khi có giọng đọc; 1 = tắt ducking")
    g.add_argument("--no-bgm", action="store_true", help="bỏ hoàn toàn âm thanh gốc")
    g.add_argument("--voice-gain", type=float, default=0.0, help="tăng/giảm âm lượng giọng đọc (dB)")
    g.add_argument("--min-pause", type=float, default=0.3, help="ngẫu nhiên hoá nhịp nghỉ giữa câu: cận dưới (giây)")
    g.add_argument("--max-pause", type=float, default=1.2, help="ngẫu nhiên hoá nhịp nghỉ giữa câu: cận trên (giây)")

    g = p.add_argument_group("điều khiển")
    g.add_argument("--script-file", type=Path, help="dùng kịch bản JSON có sẵn (bỏ qua trích frame & Gemini)")
    g.add_argument("--stop-after", type=int, choices=[0, 2, 3],
                   help="0 = chỉ xem kế hoạch chia chunk; 2 = dừng sau khi có kịch bản (để duyệt/sửa tay); "
                   "3 = dừng sau khi TTS xong (trước khi mux video)")
    g.add_argument("--force", action="store_true", help="bỏ qua TOÀN BỘ checkpoint, chạy lại từ đầu")
    g.add_argument("--keep-temp", action="store_true")
    g.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    suppress_console_windows()  # xem utils.py — bắt buộc để tiến trình con của pydub không tự bật console Windows
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_voices:
        return cmd_list_voices(args.list_voices)

    utility = args.login or args.check_web
    have_youtube = bool(args.youtube_url)
    if not utility and not have_youtube and (not args.video or not args.video.is_file()):
        parser.print_usage(sys.stderr)
        print(f"Lỗi: {'không tìm thấy file video ' + repr(str(args.video)) if args.video else 'cần chỉ định video đầu vào, --youtube-url (hoặc --login / --check-web)'}.",
              file=sys.stderr)
        return 2
    if have_youtube and args.video:
        print("Lỗi: chỉ được chọn MỘT trong hai — video đầu vào HOẶC --youtube-url, không dùng cùng lúc.", file=sys.stderr)
        return 2
    if have_youtube and args.engine != "web":
        print("Lỗi: --youtube-url chỉ dùng được với --engine web (Gemini Web).", file=sys.stderr)
        return 2

    try:
        out_dir = args.output_dir or (Path("output") / ("_web" if utility else "_youtube" if have_youtube else args.video.stem))
        cfg = Config(video_path=None if (utility or have_youtube) else args.video, youtube_url=args.youtube_url,
                     output_dir=out_dir, engine=args.engine,
                     api_key=args.api_key, gemini_model=args.gemini_model, user_data_dir=args.user_data_dir,
                     browser_channel=args.browser, headless=not args.headed, gemini_url=args.gemini_url,
                     web_model_hint=args.web_model, selectors_file=args.selectors_file,
                     web_response_timeout_sec=args.response_timeout, web_delay_between_prompts_sec=args.web_delay,
                     language=args.language, tts_engine=args.tts_engine, voice=args.voice,
                     elevenlabs_api_key=args.elevenlabs_api_key, elevenlabs_model=args.elevenlabs_model,
                     style=args.style, narrator_pov=args.narrator_pov, narrative_style=args.narrative_style,
                     extract_mode=args.mode, frame_interval_sec=args.interval,
                     scene_threshold=args.scene_threshold, frames_per_prompt=args.frames_per_prompt,
                     max_frames=args.max_frames, words_per_sec=args.wps,
                     chunk_target_sec=args.chunk_minutes * 60.0, chunk_tolerance_sec=args.chunk_tolerance_sec,
                     min_chunk_sec=args.min_chunk_sec, silence_min_sec=args.silence_min_sec,
                     silence_noise_db=args.silence_noise_db, force_chunk=args.force_chunk,
                     highlight_mode=args.highlight_mode, highlight_target_ratio=args.highlight_target_ratio,
                     highlight_frame_interval_sec=args.highlight_frame_interval,
                     max_speedup=args.max_speedup, allow_video_retime=args.allow_video_retime,
                     tts_concurrency=args.tts_concurrency, bgm_duck_ratio=args.bgm_duck,
                     keep_bgm=not args.no_bgm, voice_gain_db=args.voice_gain,
                     min_pause_sec=args.min_pause, max_pause_sec=args.max_pause, script_file=args.script_file,
                     stop_after=args.stop_after, force=args.force, keep_temp=args.keep_temp)
    except (ValueError, OSError) as e:
        print(f"Cấu hình không hợp lệ: {e}", file=sys.stderr)
        return 2

    cfg.workdir.mkdir(parents=True, exist_ok=True)
    setup_logging(cfg.log_path, args.verbose)
    log.info("Nhật ký chi tiết: %s", cfg.log_path)
    try:
        if args.login:
            return cmd_login(cfg)
        if args.check_web:
            return cmd_check_web(cfg)
        run_pipeline(cfg)
        return 0
    except (WebAuthError, GeminiWebError, PipelineError) as e:
        log.error("✖ %s", e)
        log.info("Đây là lỗi đã được xử lý - chạy LẠI ĐÚNG LỆNH NÀY sẽ tự resume từ chunk dở dang (xem checkpoint.py).")
        _print_error_report(e, cfg, "Lỗi đã xử lý (WebAuthError/GeminiWebError/PipelineError)")
        return 1
    except KeyboardInterrupt:
        log.warning("Đã huỷ bởi người dùng. Chạy lại đúng lệnh này để resume từ chunk dở dang.")
        return 130
    except Exception as e:  # noqa: BLE001
        log.exception("Lỗi không lường trước")
        _print_error_report(e, cfg, "Lỗi không lường trước (Exception)")
        return 2


def _print_error_report(exc: Exception, cfg: Config, context: str) -> None:
    """In báo cáo lỗi ĐẦY ĐỦ ra terminal + lưu file — copy trực tiếp từ terminal hoặc mở file gửi cho
    Claude, không cần tự đi ghép log/traceback/cấu hình lại với nhau."""
    from error_report import build_error_report, save_error_report
    report = build_error_report(exc, cfg, context=context)
    print("\n" + "═" * 60, file=sys.stderr)
    print("BÁO CÁO LỖI — copy TOÀN BỘ khối bên dưới (kể cả dấu ```), dán vào chat gửi Claude:", file=sys.stderr)
    print("═" * 60, file=sys.stderr)
    print(report, file=sys.stderr)
    saved = save_error_report(report, cfg)
    if saved:
        print(f"\n(Đã lưu báo cáo này vào: {saved} — mở file đó nếu lỡ cuộn mất phần trên.)", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
