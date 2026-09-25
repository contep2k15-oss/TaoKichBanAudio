"""python tests/test_elevenlabs_engine.py — dùng httpx.MockTransport, KHÔNG gọi mạng thật tới ElevenLabs."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402

from engines.base import Prosody  # noqa: E402
from engines.tts_elevenlabs import ElevenLabsTTSEngine  # noqa: E402
from utils import TTSError  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


calls = []


def handler(request: httpx.Request) -> httpx.Response:
    calls.append(request)
    body = json.loads(request.content)
    if len(calls) == 1:
        return httpx.Response(503, text="server busy")
    assert body["voice_settings"]["speed"] == 1.2, body        # rate +30% -> clamp về 1.2 (giới hạn ElevenLabs)
    assert "xi-api-key" in request.headers
    return httpx.Response(200, content=b"FAKE_MP3_BYTES")


async def main() -> None:
    engine = ElevenLabsTTSEngine("fake-key", model_id="eleven_multilingual_v2")
    check("default_voice trả về voice_id mặc định hợp lệ", len(engine.default_voice("vi")) > 0)

    import engines.tts_elevenlabs as mod
    orig_client = httpx.AsyncClient
    httpx.AsyncClient = lambda **kw: orig_client(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"})
    try:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.mp3"
            await engine.synthesize("Xin chào các bạn", "21m00Tcm4TlvDq8ikWAM", Prosody(rate="+30%"), out, timeout=10, retries=3)
            check("thất bại 503 lần đầu -> retry -> thành công, ghi đúng nội dung file", out.read_bytes() == b"FAKE_MP3_BYTES")
            check("đã gọi đúng 2 lần (1 lỗi + 1 thành công)", len(calls) == 2)
            check("endpoint đúng chuẩn ElevenLabs (v1/text-to-speech/{voice_id})", "/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM" in str(calls[-1].url))
    finally:
        httpx.AsyncClient = orig_client
        del mod

    # thiếu API key -> lỗi rõ ràng ngay khi khởi tạo, không đợi tới lúc gọi mạng
    try:
        ElevenLabsTTSEngine("")
        raise AssertionError("phải TTSError")
    except TTSError:
        print("  ✓ thiếu API key -> TTSError ngay khi khởi tạo")


asyncio.run(main())
print("TẤT CẢ PASS ✔")
