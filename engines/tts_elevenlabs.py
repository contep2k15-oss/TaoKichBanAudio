"""Adapter: ElevenLabs TTS (dịch vụ trả phí, giọng rất tự nhiên) thành BaseTTSEngine.

Endpoint & tham số dựa theo tài liệu ElevenLabs hiện hành (https://elevenlabs.io/docs/api-reference/text-to-speech):
  POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
  header: xi-api-key: <key>
  body:   {"text": ..., "model_id": "eleven_multilingual_v2",
           "voice_settings": {"stability", "similarity_boost", "style", "speed"}}
  `voice_settings.speed` nhận 0.7–1.2 (mặc định 1.0): dùng để diễn dịch `prosody.rate`.

LƯU Ý QUAN TRỌNG: sandbox phát triển dự án này không có quyền truy cập mạng tới api.elevenlabs.io nên
adapter dưới đây CHƯA được gọi thử với API thật — chỉ được kiểm tra cú pháp/luồng lỗi. Trước khi dùng
thật, hãy thử với 1 câu ngắn (`--tts-engine elevenlabs --stop-after 3` trên một video test) và đối chiếu
lại tài liệu mới nhất, vì ElevenLabs có thể đổi endpoint/tham số theo thời gian.
"""
from __future__ import annotations

import asyncio
import logging

from engines.base import BaseTTSEngine, Prosody
from utils import TTSError

log = logging.getLogger("svvc.tts.elevenlabs")

API_BASE = "https://api.elevenlabs.io/v1"
# Giọng đa ngôn ngữ mặc định của ElevenLabs ("Aria"); tiếng Việt cần model đa ngôn ngữ (multilingual_v2/v3).
DEFAULT_VOICE_ID = "9BWtsMINqrJLrRacOk9x"
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ElevenLabsTTSEngine(BaseTTSEngine):
    name = "elevenlabs"

    def __init__(self, api_key: str, model_id: str = "eleven_multilingual_v2",
                 stability: float = 0.5, similarity_boost: float = 0.75, style: float = 0.3) -> None:
        if not api_key:
            raise TTSError("Thiếu ELEVENLABS_API_KEY.")
        self.api_key, self.model_id = api_key, model_id
        self.stability, self.similarity_boost, self.style = stability, similarity_boost, style

    def default_voice(self, language: str) -> str:
        # ElevenLabs không có "giọng mặc định theo ngôn ngữ" công khai qua tên cố định như Edge-TTS;
        # model đa ngôn ngữ tự nhận diện ngôn ngữ từ `text`. Trả về voice_id mặc định, khuyến khích
        # người dùng tự chọn qua --voice (lấy voice_id bằng GET /v1/voices).
        return DEFAULT_VOICE_ID

    async def synthesize(self, text, voice, prosody: Prosody, out_path, *, timeout: float, retries: int) -> None:
        try:
            import httpx
        except ImportError as e:
            raise TTSError("Chưa cài httpx (cần cho ElevenLabs): pip install httpx") from e

        voice_id = voice or DEFAULT_VOICE_ID
        speed = max(0.7, min(1.2, round(prosody.rate_ratio(), 2)))     # ElevenLabs giới hạn speed trong [0.7, 1.2]
        payload = {"text": text, "model_id": self.model_id,
                   "voice_settings": {"stability": self.stability, "similarity_boost": self.similarity_boost,
                                      "style": self.style, "speed": speed}}
        url = f"{API_BASE}/text-to-speech/{voice_id}?output_format=mp3_44100_128"
        headers = {"xi-api-key": self.api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"}

        last: Exception | None = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, retries + 1):
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code == 200 and resp.content:
                        out_path.write_bytes(resp.content)
                        return
                    body = resp.text[:300]
                    retryable = resp.status_code in _RETRYABLE_STATUS
                    last = TTSError(f"ElevenLabs HTTP {resp.status_code}: {body}")
                    if not retryable:
                        raise last
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last = TTSError(f"Lỗi mạng khi gọi ElevenLabs: {e}")
                except TTSError:
                    raise
                wait = min(20.0, 1.5 * 2 ** (attempt - 1))
                log.warning("ElevenLabs lỗi (lần %d/%d): %s%s", attempt, retries, last,
                            f" — thử lại sau {wait:.1f}s" if attempt < retries else "")
                if attempt < retries:
                    await asyncio.sleep(wait)
        raise TTSError(f"ElevenLabs thất bại sau {retries} lần: {last}") from last
