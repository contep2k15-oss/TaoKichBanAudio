"""Option A — Gemini API (google-genai). Cùng giao diện `ask_json()` với GeminiWebDriver để dùng thay thế nhau."""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any, Sequence

from config import Config
from response_parser import ResponseKind, analyze_response
from utils import GeminiError

log = logging.getLogger("svvc.api")
_RETRYABLE_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_NETWORK_ERROR_NAMES = {"ConnectError", "ConnectTimeout", "ReadTimeout", "ReadError", "WriteError",
                        "RemoteProtocolError", "PoolTimeout", "TimeoutException", "ServerError"}


class GeminiApiDriver:
    def __init__(self, cfg: Config) -> None:
        if not cfg.api_key:
            raise GeminiError("Thiếu API key. Đặt GEMINI_API_KEY (hoặc file .env / --api-key), "
                              "lấy key tại https://aistudio.google.com/apikey")
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise GeminiError("Chưa cài SDK Gemini: pip install google-genai") from e
        self._types = types
        self.client = genai.Client(api_key=cfg.api_key)
        self.model = cfg.gemini_model
        self.max_retries = max(1, cfg.gemini_max_retries)
        log.info("Gemini API model: %s", self.model)

    # tương thích giao diện với GeminiWebDriver
    def __enter__(self) -> "GeminiApiDriver":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def ask_json(self, prompt: str, images: Sequence[Path] = (), *, label: str = "ask") -> list[dict]:
        contents: list[Any] = [prompt] + [
            self._types.Part.from_bytes(data=Path(p).read_bytes(), mime_type="image/jpeg") for p in images]
        cfg = self._types.GenerateContentConfig(
            response_mime_type="application/json",
            automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True))
        best_partial: list[dict] | None = None
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            code: int | None = None
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=cfg)
                try:
                    text = resp.text or ""
                except Exception:  # noqa: BLE001  (bị chặn an toàn → .text có thể ném lỗi)
                    text = ""
                payload, diag = analyze_response([text])
                if diag.ok and payload is not None:
                    return payload
                if diag.kind is ResponseKind.TRUNCATED_JSON and payload:
                    best_partial = payload
                last_err = diag.to_exception()
                log.warning("[%s] %s (lần %d/%d)", label, diag.message, attempt, self.max_retries)
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "code", None) or getattr(e, "status_code", None)
                code = code if isinstance(code, int) else None
                retryable = (code in _RETRYABLE_CODES) if code is not None else (
                    isinstance(e, (ConnectionError, TimeoutError)) or type(e).__name__ in _NETWORK_ERROR_NAMES)
                if not retryable:
                    raise GeminiError(f"[{label}] Gemini API từ chối yêu cầu{f' (HTTP {code})' if code else ''}: {e}") from e
                last_err = e
                log.warning("[%s] Lỗi tạm thời (lần %d/%d)%s: %s", label, attempt, self.max_retries,
                            f" HTTP {code}" if code else "", str(e)[:200])
            if attempt < self.max_retries:
                delay = min(90.0, (15.0 * attempt) if code == 429 else (2.0 ** attempt)) + random.uniform(0, 1)
                log.info("[%s] Chờ %.1fs rồi thử lại...", label, delay)
                time.sleep(delay)
        if best_partial:
            log.warning("[%s] Dùng %d phần tử JSON khôi phục được từ phản hồi bị cắt cụt.", label, len(best_partial))
            return best_partial
        raise GeminiError(f"[{label}] Thất bại sau {self.max_retries} lần thử: {last_err}") from last_err
