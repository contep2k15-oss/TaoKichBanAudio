"""Adapter: Edge-TTS (Microsoft, miễn phí) thành BaseTTSEngine."""
from __future__ import annotations

import asyncio
import logging

from engines.base import BaseTTSEngine, Prosody
from utils import TTSError

log = logging.getLogger("svvc.tts.edge")

DEFAULT_VOICES = {"vi": "vi-VN-HoaiMyNeural", "en": "en-US-AriaNeural"}


class EdgeTTSEngine(BaseTTSEngine):
    name = "edge"

    def default_voice(self, language: str) -> str:
        return DEFAULT_VOICES.get(language, DEFAULT_VOICES["en"])

    async def synthesize(self, text, voice, prosody: Prosody, out_path, *, timeout: float, retries: int) -> None:
        try:
            import edge_tts
        except ImportError as e:
            raise TTSError("Chưa cài edge-tts: pip install edge-tts") from e

        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                comm = edge_tts.Communicate(text, voice, rate=prosody.rate, volume=prosody.volume, pitch=prosody.pitch)
                await asyncio.wait_for(comm.save(str(out_path)), timeout=timeout)
                if out_path.is_file() and out_path.stat().st_size > 0:
                    return
                raise TTSError("Edge-TTS tạo file rỗng")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  (mạng, timeout, NoAudioReceived, 403...)
                last = e
                out_path.unlink(missing_ok=True)
                wait = min(20.0, 1.5 * 2 ** (attempt - 1))
                log.warning("Edge-TTS lỗi (lần %d/%d): %s: %s%s", attempt, retries, type(e).__name__, e,
                            f" — thử lại sau {wait:.1f}s" if attempt < retries else "")
                if attempt < retries:
                    await asyncio.sleep(wait)
        hint = " (Gợi ý: nếu lỗi 403, chạy `pip install -U edge-tts`)" if "403" in str(last) else ""
        raise TTSError(f"Edge-TTS thất bại sau {retries} lần: {last}{hint}") from last
