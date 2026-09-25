"""Cấu hình trung tâm."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"          # chỉ dùng cho --engine api
DEFAULT_GEMINI_URL = "https://gemini.google.com/app?hl=en"   # ?hl=en: ép giao diện tiếng Anh cho selector ổn định
DEFAULT_PROFILE_DIR = Path.home() / ".silent_video_voiceover" / "gemini_profile"

DEFAULT_VOICES = {"vi": "vi-VN-HoaiMyNeural", "en": "en-US-AriaNeural"}
LANGUAGE_NAMES = {"vi": "tiếng Việt", "en": "English"}
DEFAULT_WORDS_PER_SEC = {"vi": 3.2, "en": 2.5}      # "từ" tiếng Việt = tiếng (âm tiết) tách bằng khoảng trắng


@dataclass
class Config:
    video_path: Path | None = None
    youtube_url: str | None = None           # thay cho video_path: gửi thẳng link cho Gemini Web, tự tải về sau (xem youtube_source.py)
    output_dir: Path = Path("output")
    workdir: Path | None = None

    # ── Engine ────────────────────────────────────────────
    engine: str = "web"                      # "web" (Playwright) | "api" (google-genai)

    # ── Option A: Gemini API ──────────────────────────────
    api_key: str | None = None
    gemini_model: str | None = None          # None → $GEMINI_MODEL → DEFAULT_GEMINI_MODEL
    gemini_max_retries: int = 5

    # ── Option B: Gemini Web ──────────────────────────────
    user_data_dir: Path | None = None        # None → $SVVC_USER_DATA_DIR → ~/.silent_video_voiceover/gemini_profile
    browser_channel: str = "chrome"          # "chrome" | "msedge" | "chromium" (bản đi kèm Playwright)
    headless: bool = True
    gemini_url: str = DEFAULT_GEMINI_URL
    web_model_hint: str = "Flash|Fast|Nhanh" # regex khớp tên model trên giao diện
    web_response_timeout_sec: float = 240.0
    web_upload_timeout_sec: float = 90.0
    web_stable_sec: float = 2.5              # văn bản không đổi ≥ ngần này giây mới coi là render xong
    web_retries: int = 3
    web_delay_between_prompts_sec: float = 3.0
    selectors_file: Path | None = None       # JSON ghi đè/bổ sung selector khi Gemini đổi giao diện

    # ── Ngôn ngữ / phong cách ─────────────────────────────
    language: str = "vi"
    voice: str | None = None
    style: str = "tự nhiên, truyền cảm, như đang kể chuyện cho người xem"
    narrator_pov: str = ""                   # ngôi kể (vd "Tôi", "Chúng ta", "Người quan sát") — rỗng = để Gemini tự chọn
    narrative_style: str = "natural"         # "natural" | "humorous" | "formal" | "fantasy_inspiring"

    # ── Trích xuất frame ──────────────────────────────────
    extract_mode: str = "interval"           # "interval" | "scene"
    frame_interval_sec: float = 2.5
    scene_threshold: float = 0.35            # khoảng cách Bhattacharyya giữa 2 histogram (0-1)
    min_scene_sec: float = 1.5
    max_scene_sec: float = 6.0
    max_frames: int = 400
    max_frames_per_window: int = 2
    frame_max_width: int = 960
    jpeg_quality: int = 82
    frames_per_prompt: int | None = None     # None → 8 (web; giới hạn upload) / 16 (api)

    # ── Kịch bản ──────────────────────────────────────────
    words_per_sec: float | None = None
    wps_tolerance: float = 1.15
    min_segment_sec: float = 1.0
    max_rewrite_rounds: int = 2

    # ── TTS ───────────────────────────────────────────────
    tts_concurrency: int = 4
    tts_retries: int = 4
    tts_timeout_sec: float = 60.0
    max_speedup: float = 1.25
    trim_silence: bool = True

    # ── Ghép & mux ────────────────────────────────────────
    keep_bgm: bool = True
    bgm_duck_ratio: float = 0.7
    duck_ramp_ms: int = 80
    voice_gain_db: float = 0.0
    audio_bitrate: str = "192k"
    min_pause_sec: float = 0.3               # ngẫu nhiên hoá nhịp nghỉ giữa câu: cận dưới (giây)
    max_pause_sec: float = 1.2               # ngẫu nhiên hoá nhịp nghỉ giữa câu: cận trên (giây)

    # ── TTS engine (Edge-TTS / ElevenLabs — xem engines/, engine_factory.py) ──
    tts_engine: str = "edge"                 # "edge" | "elevenlabs"
    elevenlabs_api_key: str | None = None
    elevenlabs_model: str = "eleven_multilingual_v2"

    # ── Phân đoạn động cho VIDEO DÀI (chunk_planner.py, silence_detector.py) ──
    chunk_target_sec: float = 240.0          # độ dài mục tiêu mỗi macro-chunk (~4 phút)
    chunk_tolerance_sec: float = 45.0        # dung sai tìm điểm cắt an toàn quanh mốc mục tiêu
    min_chunk_sec: float = 60.0              # không chunk nào ngắn hơn ngần này (trừ chunk cuối)
    silence_min_sec: float = 0.6             # khoảng lặng tối thiểu để tính là "điểm cắt an toàn"
    silence_noise_db: float = -30.0          # ngưỡng coi là "im lặng" (dBFS) khi quét silencedetect
    allow_video_retime: bool = False         # last-resort: cho phép làm CHẬM khung hình khi audio quá dài
                                              # kể cả sau khi tăng tốc 1.25x và đã lấn hết khoảng trống

    # ── Chỉ giữ cảnh hay (highlight) — GIAI ĐOẠN 0, chạy TRƯỚC chunk_planner để đỡ tốn tài nguyên ──
    highlight_mode: bool = False             # bật: cắt video xuống chỉ còn các đoạn "đắt giá" trước khi làm gì khác
    highlight_target_ratio: float = 0.4      # giữ lại khoảng bao nhiêu % tổng thời lượng gốc (0-1)
    highlight_frame_interval_sec: float = 6.0  # lấy mẫu THƯA hơn hẳn bình thường cho bước quét nhanh này

    # ── Điều khiển ────────────────────────────────────────
    script_file: Path | None = None
    youtube_url: str | None = None           # thay vì video local: gửi thẳng link cho Gemini Web viết kịch bản
    force: bool = False
    force_chunk: int | None = None           # chỉ làm lại (bỏ qua checkpoint) MỘT chunk cụ thể, theo index
    stop_after: int | None = None
    keep_temp: bool = False

    def __post_init__(self) -> None:
        if self.video_path:
            self.video_path = Path(self.video_path).expanduser().resolve()
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        self.workdir = Path(self.workdir).expanduser().resolve() if self.workdir else self.output_dir / "work"
        if self.script_file:
            self.script_file = Path(self.script_file).expanduser().resolve()
        if self.selectors_file:
            self.selectors_file = Path(self.selectors_file).expanduser().resolve()
        env_dir = os.getenv("SVVC_USER_DATA_DIR")
        self.user_data_dir = Path(self.user_data_dir or env_dir or DEFAULT_PROFILE_DIR).expanduser().resolve()

        if self.engine not in ("web", "api"):
            raise ValueError("engine phải là 'web' hoặc 'api'")
        if self.browser_channel not in ("chrome", "msedge", "chromium"):
            raise ValueError("browser_channel phải là 'chrome', 'msedge' hoặc 'chromium'")
        if self.language not in DEFAULT_VOICES:
            raise ValueError(f"Ngôn ngữ '{self.language}' chưa hỗ trợ (có: {', '.join(DEFAULT_VOICES)}).")
        if self.extract_mode not in ("interval", "scene"):
            raise ValueError("extract_mode phải là 'interval' hoặc 'scene'")
        if not 1.0 <= self.max_speedup <= 2.0:
            raise ValueError("max_speedup phải nằm trong [1.0, 2.0] (giới hạn của bộ lọc atempo)")
        if not 0.0 < self.bgm_duck_ratio <= 1.0:
            raise ValueError("bgm_duck_ratio phải nằm trong (0, 1]")
        if self.tts_engine not in ("edge", "elevenlabs"):
            raise ValueError("tts_engine phải là 'edge' hoặc 'elevenlabs'")
        if self.chunk_target_sec <= 0 or self.chunk_tolerance_sec < 0 or self.min_chunk_sec <= 0:
            raise ValueError("chunk_target_sec/min_chunk_sec phải > 0, chunk_tolerance_sec phải ≥ 0")
        if self.min_chunk_sec > self.chunk_target_sec:
            raise ValueError("min_chunk_sec không được lớn hơn chunk_target_sec")
        if self.min_pause_sec < 0 or self.max_pause_sec < self.min_pause_sec:
            raise ValueError("cần 0 ≤ min_pause_sec ≤ max_pause_sec")
        if self.narrative_style not in ("natural", "humorous", "formal", "fantasy_inspiring"):
            raise ValueError("narrative_style phải là 'natural', 'humorous', 'formal' hoặc 'fantasy_inspiring'")
        if not 0.0 < self.highlight_target_ratio <= 1.0:
            raise ValueError("highlight_target_ratio phải nằm trong (0, 1]")
        if self.youtube_url and self.highlight_mode:
            raise ValueError("Chưa hỗ trợ dùng đồng thời youtube_url + highlight_mode: kịch bản lấy qua "
                             "link YouTube dùng mốc thời gian TUYỆT ĐỐI của video gốc, sẽ bị lệch nếu video "
                             "sau đó bị cắt bởi highlight_mode. Tắt một trong hai tuỳ chọn.")
        if self.youtube_url and self.video_path:
            raise ValueError("Vừa có video_path vừa có youtube_url — chỉ được chọn MỘT nguồn video.")
        if self.youtube_url and self.engine != "web":
            raise ValueError("--youtube-url hiện chỉ hỗ trợ --engine web (dán link thẳng vào khung chat Gemini "
                             "Web). Với --engine api, việc chỉ nhét link vào văn bản KHÔNG khiến Gemini thực sự "
                             "xem video — cần cấu trúc request khác (interactions API) mà bản này chưa hỗ trợ.")
        if self.youtube_url and self.script_file:
            raise ValueError("Chỉ được chọn MỘT trong hai: --youtube-url hoặc --script-file, không dùng cùng lúc.")

        if not self.voice and self.tts_engine == "edge":     # ElevenLabs: để None, dùng engine.default_voice() lúc chạy
            self.voice = DEFAULT_VOICES[self.language]
        self.words_per_sec = self.words_per_sec or DEFAULT_WORDS_PER_SEC[self.language]
        self.api_key = self.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.gemini_model = self.gemini_model or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        self.elevenlabs_api_key = self.elevenlabs_api_key or os.getenv("ELEVENLABS_API_KEY")
        if self.frames_per_prompt is None:
            self.frames_per_prompt = 8 if self.engine == "web" else 16
        # Gemini Web giới hạn số tệp đính kèm mỗi tin nhắn (~10)
        if self.engine == "web" and self.frames_per_prompt > 10:
            raise ValueError("Gemini Web chỉ nhận tối đa ~10 ảnh mỗi prompt: đặt --frames-per-prompt ≤ 10.")

    @property
    def language_name(self) -> str:
        return LANGUAGE_NAMES[self.language]

    # ── Đường dẫn artifact ────────────────────────────────
    @property
    def frames_dir(self) -> Path: return self.workdir / "frames"
    @property
    def batches_dir(self) -> Path: return self.workdir / "script_batches"
    @property
    def web_debug_dir(self) -> Path: return self.workdir / "web_debug"
    @property
    def tts_dir(self) -> Path: return self.workdir / "tts"
    @property
    def chunk_audio_dir(self) -> Path: return self.workdir / "chunk_audio"          # voiceover đã ghép, mỗi chunk 1 file
    @property
    def chunk_reports_dir(self) -> Path: return self.workdir / "chunk_reports"      # sync_report.json riêng từng chunk
    @property
    def log_path(self) -> Path: return self.workdir / "pipeline.log"
    @property
    def script_path(self) -> Path: return self.output_dir / "script.json"
    @property
    def sync_report_path(self) -> Path: return self.output_dir / "sync_report.json"
    @property
    def voiceover_path(self) -> Path: return self.output_dir / "final_voiceover.mp3"
    @property
    def final_video_path(self) -> Path: return self.output_dir / "output_dubbed_video.mp4"

    def ensure_dirs(self) -> None:
        for d in (self.output_dir, self.workdir, self.frames_dir, self.batches_dir, self.tts_dir):
            d.mkdir(parents=True, exist_ok=True)
