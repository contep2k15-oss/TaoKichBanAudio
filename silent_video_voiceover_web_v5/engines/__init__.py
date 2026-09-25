"""Các adapter mỏng chuẩn hoá Vision engine (Gemini Web/API) và TTS engine (Edge-TTS/ElevenLabs...)
về hai giao diện trừu tượng `BaseVisionEngine` và `BaseTTSEngine` (xem `engines/base.py`).

Nhờ vậy pipeline (chunk_planner → script_generator → tts_engine → audio_assembler → video_muxer)
KHÔNG biết và không cần biết đang nói chuyện với Gemini Web hay Gemini API, Edge-TTS hay ElevenLabs —
chỉ gọi qua interface chung. Đổi dịch vụ = đổi engine ở lớp này, không đụng tới FFmpeg/pydub bên dưới.
"""
