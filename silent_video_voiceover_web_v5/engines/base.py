"""Base Class / Interface cho hai loại engine có thể hoán đổi cho nhau.

  • BaseVisionEngine — "hỏi Gemini (hoặc mô hình thị giác khác) và nhận về JSON".
    Cả GeminiWebDriver (Playwright) và GeminiApiDriver (google-genai) đều đã có sẵn phương thức
    `ask_json(prompt, images, label=...) -> list[dict]` cùng chữ ký, nên adapter trong module này
    chỉ cần bọc lại (composition) để chính thức hoá hợp đồng bằng ABC — không đổi logic bên trong.

  • BaseTTSEngine — "đọc một câu văn bản thành file audio", tách biệt khỏi toàn bộ phần đo thời lượng /
    time-stretch / ghép timeline (nằm ở `time_fit.py` và `tts_engine.py`, dùng chung cho MỌI engine).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Sequence


class BaseVisionEngine(ABC):
    """Giao diện chung cho engine thị giác (Gemini Web / Gemini API / ... trong tương lai)."""

    name: str = "vision"

    @abstractmethod
    def ask_json(self, prompt: str, images: Sequence[Path] = (), *, label: str = "ask") -> list[dict]:
        """Gửi prompt (+ ảnh tuỳ chọn) và trả về mảng JSON đã bóc tách, đã thử lại khi lỗi tạm thời."""

    def close(self) -> None:
        """Giải phóng tài nguyên (đóng trình duyệt/HTTP client). Mặc định không làm gì."""

    def __enter__(self) -> "BaseVisionEngine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class BaseTTSEngine(ABC):
    """Giao diện chung cho engine TTS (Edge-TTS / ElevenLabs / ... trong tương lai).

    Hợp đồng: `synthesize()` chỉ có nhiệm vụ DUY NHẤT là ghi ra một file audio tại `out_path` ứng với
    `text`; nó KHÔNG biết gì về thời lượng khung cảnh, time-stretch hay vị trí trên timeline — những việc
    đó nằm ở `time_fit.py` (tính tỉ lệ, quyết định atempo) và `audio_assembler.py` (đặt lên timeline).
    Nhờ tách bạch này, đổi Edge-TTS ↔ ElevenLabs không ảnh hưởng gì tới pipeline FFmpeg phía sau.
    """

    name: str = "tts"

    @abstractmethod
    async def synthesize(self, text: str, voice: str, prosody: "Prosody", out_path: Path, *,
                         timeout: float, retries: int) -> None:
        """Tổng hợp `text` thành file audio tại `out_path`. Tự retry nội bộ theo `retries`/`timeout`.
        Ném `utils.TTSError` (hoặc lớp con) nếu thất bại sau khi đã thử lại."""

    @abstractmethod
    def default_voice(self, language: str) -> str:
        """Trả về voice mặc định hợp lý cho ngôn ngữ (vd 'vi', 'en')."""


class Prosody:
    """Tham số biểu cảm trung lập giữa các engine (không phải mọi engine hỗ trợ đủ 3 trường).

    - rate/volume: chuỗi phần trăm ký hiệu dấu, vd '+10%', '-5%' (quy ước của Edge-TTS SSML).
    - pitch: chuỗi Hz ký hiệu dấu, vd '+3Hz' (chỉ Edge-TTS dùng; ElevenLabs không có tham số cao độ công khai).
    Mỗi adapter TỰ diễn dịch các trường này theo khả năng của dịch vụ mình bọc (xem tts_edge.py, tts_elevenlabs.py).
    """

    __slots__ = ("rate", "pitch", "volume")

    def __init__(self, rate: str = "+0%", pitch: str = "+0Hz", volume: str = "+0%") -> None:
        self.rate, self.pitch, self.volume = rate, pitch, volume

    def rate_ratio(self) -> float:
        """'+10%' → 1.10, '-8%' → 0.92 — dùng cho engine chỉ nhận hệ số tốc độ thay vì chuỗi %."""
        return 1.0 + float(self.rate.strip().rstrip("%")) / 100.0

    def __repr__(self) -> str:  # pragma: no cover - tiện debug
        return f"Prosody(rate={self.rate!r}, pitch={self.pitch!r}, volume={self.volume!r})"
