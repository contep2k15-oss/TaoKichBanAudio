"""Adapter: bọc GeminiWebDriver (Playwright) thành BaseVisionEngine."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from engines.base import BaseVisionEngine


class GeminiWebVisionEngine(BaseVisionEngine):
    name = "gemini-web"

    def __init__(self, driver: Any) -> None:
        """`driver`: một GeminiWebDriver ĐÃ start() và ensure_logged_in() (xem engine_factory.py)."""
        self._driver = driver

    def ask_json(self, prompt: str, images: Sequence[Path] = (), *, label: str = "ask") -> list[dict]:
        return self._driver.ask_json(prompt, images, label=label)

    def close(self) -> None:
        self._driver.close()

    @property
    def driver(self) -> Any:
        """Lộ ra driver Playwright gốc — dùng cho các lệnh CLI cần thao tác trực tiếp (vd `--check-web`,
        `--login`), nằm ngoài phạm vi giao diện BaseVisionEngine (vốn chỉ có ask_json/close)."""
        return self._driver
