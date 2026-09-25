"""Adapter: bọc GeminiApiDriver (google-genai) thành BaseVisionEngine."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from config import Config
from engines.base import BaseVisionEngine
from gemini_api_driver import GeminiApiDriver


class GeminiApiVisionEngine(BaseVisionEngine):
    name = "gemini-api"

    def __init__(self, cfg: Config) -> None:
        self._driver = GeminiApiDriver(cfg)

    def ask_json(self, prompt: str, images: Sequence[Path] = (), *, label: str = "ask") -> list[dict]:
        return self._driver.ask_json(prompt, images, label=label)

    def close(self) -> None:
        return None
