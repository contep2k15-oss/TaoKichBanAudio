"""Nhà máy tạo Vision engine / TTS engine đúng loại theo cấu hình (đăng ký kiểu Strategy Pattern).

Đây là NƠI DUY NHẤT trong dự án biết tên chuỗi 'web'/'api'/'edge'/'elevenlabs' ứng với class nào.
Muốn thêm dịch vụ mới (vd Claude Vision, Azure TTS...): viết class implement BaseVisionEngine/BaseTTSEngine
rồi đăng ký thêm một dòng vào hai dict *_REGISTRY bên dưới — không phải sửa bất kỳ nơi nào khác.
"""
from __future__ import annotations

import logging
import sys
from typing import Callable

from config import Config
from engines.base import BaseTTSEngine, BaseVisionEngine
from utils import GeminiWebError, PipelineError, WebAuthError

log = logging.getLogger("svvc.engine_factory")


def _stdin_is_interactive() -> bool:
    """`sys.stdin.isatty()` an toàn: khi chạy app dạng ĐÓNG GÓI KHÔNG CÓ CONSOLE (windowed .exe, hoặc bên
    trong máy chủ Streamlit của GUI), `sys.stdin` là `None` — gọi thẳng `.isatty()` sẽ crash với
    `AttributeError: 'NoneType' object has no attribute 'isatty'`. Không có console nghĩa là chắc chắn
    KHÔNG thể hỏi người dùng gõ Enter/nhập liệu được, nên coi là "không tương tác" (False), an toàn."""
    return sys.stdin is not None and sys.stdin.isatty()


# ════════════════════════════════════════════════════════════
# Vision engine (Gemini Web / Gemini API)
# ════════════════════════════════════════════════════════════
def _profile_initialized(cfg: Config) -> bool:
    return cfg.user_data_dir.is_dir() and any(cfg.user_data_dir.iterdir())


def _open_gemini_web(cfg: Config, interactive: bool | None = None) -> BaseVisionEngine:
    from engines.vision_gemini_web import GeminiWebVisionEngine
    from gemini_web_driver import GeminiWebDriver, interactive_login
    interactive = _stdin_is_interactive() if interactive is None else interactive

    if not _profile_initialized(cfg):
        log.info("Chưa có profile đăng nhập tại %s.", cfg.user_data_dir)
        if not interactive:
            raise WebAuthError("Chưa đăng nhập Gemini Web. Chạy `python main.py --login` một lần trước.")
        interactive_login(cfg)

    drv = GeminiWebDriver(cfg)
    drv.start()
    try:
        drv.ensure_logged_in()
        return GeminiWebVisionEngine(drv)
    except WebAuthError:
        drv.close()
        if interactive and input("Phiên chưa hợp lệ. Đăng nhập lại ngay? [Y/n] ").strip().lower() in ("", "y", "yes", "c", "có"):
            if interactive_login(cfg):
                drv = GeminiWebDriver(cfg)
                drv.start()
                drv.ensure_logged_in()
                return GeminiWebVisionEngine(drv)
        raise
    except BaseException:
        drv.close()
        raise


def _open_gemini_api(cfg: Config) -> BaseVisionEngine:
    from engines.vision_gemini_api import GeminiApiVisionEngine
    return GeminiApiVisionEngine(cfg)


VISION_REGISTRY: dict[str, Callable[[Config], BaseVisionEngine]] = {
    "web": _open_gemini_web,
    "api": _open_gemini_api,
}


def open_vision_engine(cfg: Config, interactive: bool | None = None) -> BaseVisionEngine:
    factory = VISION_REGISTRY.get(cfg.engine)
    if factory is None:
        raise PipelineError(f"engine='{cfg.engine}' không hợp lệ. Các lựa chọn: {', '.join(VISION_REGISTRY)}")
    try:
        return factory(cfg, interactive) if cfg.engine == "web" else factory(cfg)
    except GeminiWebError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"Không mở được vision engine '{cfg.engine}': {e}") from e


# ════════════════════════════════════════════════════════════
# TTS engine (Edge-TTS / ElevenLabs)
# ════════════════════════════════════════════════════════════
def _make_edge(cfg: Config) -> BaseTTSEngine:
    from engines.tts_edge import EdgeTTSEngine
    return EdgeTTSEngine()


def _make_elevenlabs(cfg: Config) -> BaseTTSEngine:
    from engines.tts_elevenlabs import ElevenLabsTTSEngine
    if not cfg.elevenlabs_api_key:
        raise PipelineError("Thiếu ELEVENLABS_API_KEY (hoặc --elevenlabs-api-key) cho --tts-engine elevenlabs.")
    return ElevenLabsTTSEngine(cfg.elevenlabs_api_key, model_id=cfg.elevenlabs_model)


TTS_REGISTRY: dict[str, Callable[[Config], BaseTTSEngine]] = {
    "edge": _make_edge,
    "elevenlabs": _make_elevenlabs,
}


def get_tts_engine(cfg: Config) -> BaseTTSEngine:
    factory = TTS_REGISTRY.get(cfg.tts_engine)
    if factory is None:
        raise PipelineError(f"tts_engine='{cfg.tts_engine}' không hợp lệ. Các lựa chọn: {', '.join(TTS_REGISTRY)}")
    return factory(cfg)
