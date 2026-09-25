"""BƯỚC 5 — video_muxer: Audio Ducking + Video Muxing (FFmpeg).

  • Video gốc có âm thanh (BGM) → tách ra, hạ âm lượng còn `bgm_duck_ratio` (mặc định 70%) đúng tại các khoảng có giọng
    đọc (ramp mượt, không "click"), cộng giọng đọc, rồi mux.
  • Video gốc không có âm thanh → mux thẳng giọng đọc.
Ducking/mixing làm bằng numpy theo khoảng thời gian chính xác của kịch bản (không cần filter_complex dài).
Lệnh FFmpeg cuối chỉ có 2 input: copy luồng video + mã hoá AAC (dựng bằng ffmpeg-python).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from pydub import AudioSegment

from audio_assembler import PlacedSegment
from config import Config
from utils import MediaInfo, MediaToolError, PipelineError, run_cmd

log = logging.getLogger("svvc.mux")

MIX_SAMPLE_RATE = 44100
PEAK_LIMIT = 32767 * 0.97
MERGE_GAP_MS = 300            # hai đoạn thoại cách nhau < 300ms → giữ BGM ở mức thấp liên tục (tránh "bơm" âm lượng)
SILENT_BGM_DBFS = -65.0


# ── numpy helpers ───────────────────────────────────────────
def _to_float(seg: AudioSegment) -> np.ndarray:
    arr = np.frombuffer(seg.set_sample_width(2).raw_data, dtype=np.int16).astype(np.float32)
    return arr.reshape(-1, seg.channels)


def _from_float(arr: np.ndarray, sample_rate: int) -> AudioSegment:
    data = np.clip(arr, -32768, 32767).astype(np.int16)
    return AudioSegment(data=data.tobytes(), sample_width=2, frame_rate=sample_rate, channels=arr.shape[1])


def _merge_ranges(ranges: list[tuple[int, int]], gap_ms: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for s, e in sorted(ranges):
        if merged and s - merged[-1][1] < gap_ms:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def build_duck_envelope(n_samples: int, sample_rate: int, ranges_ms: list[tuple[int, int]],
                        ratio: float, ramp_ms: int) -> np.ndarray:
    """Envelope gain: 1.0 bình thường → `ratio` trong vùng có giọng đọc, chuyển mượt (attack/release)."""
    env = np.ones(n_samples, dtype=np.float32)
    ramp = max(1, int(sample_rate * ramp_ms / 1000))
    for s_ms, e_ms in _merge_ranges(ranges_ms, MERGE_GAP_MS):
        s = min(n_samples, max(0, int(s_ms * sample_rate / 1000)))
        e = min(n_samples, max(s, int(e_ms * sample_rate / 1000)))
        a, b = max(0, s - ramp), min(n_samples, e + ramp)
        if s > a:
            env[a:s] = np.minimum(env[a:s], np.linspace(1.0, ratio, s - a, dtype=np.float32))
        env[s:e] = np.minimum(env[s:e], ratio)
        if b > e:
            env[e:b] = np.minimum(env[e:b], np.linspace(ratio, 1.0, b - e, dtype=np.float32))
    return env


def duck_and_mix(bgm: AudioSegment, voice: AudioSegment, ranges_ms: list[tuple[int, int]],
                 total_ms: int, cfg: Config) -> AudioSegment:
    sr, ch = bgm.frame_rate, bgm.channels
    n = int(round(total_ms * sr / 1000))

    def fit(a: np.ndarray) -> np.ndarray:      # khớp độ dài video
        return a[:n] if len(a) >= n else np.vstack([a, np.zeros((n - len(a), a.shape[1]), dtype=np.float32)])

    b = fit(_to_float(bgm))
    v = fit(_to_float(voice.set_frame_rate(sr).set_channels(ch))) * 10 ** (cfg.voice_gain_db / 20.0)
    if cfg.bgm_duck_ratio < 1.0 and ranges_ms:
        b = b * build_duck_envelope(n, sr, ranges_ms, cfg.bgm_duck_ratio, cfg.duck_ramp_ms)[:, None]
    mix = b + v
    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > PEAK_LIMIT:                       # tránh clipping méo tiếng
        log.warning("Tổng âm lượng vượt ngưỡng (peak=%.0f) → giảm chung %.1f dB.", peak, 20 * np.log10(peak / PEAK_LIMIT))
        mix = mix * (PEAK_LIMIT / peak)
    return _from_float(mix, sr)


# ── điểm vào ────────────────────────────────────────────────
def mux_video(cfg: Config, media: MediaInfo, voice_track: AudioSegment, placed: list[PlacedSegment]) -> None:
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    total_ms = media.duration_ms
    ranges = [(p.placed_start_ms, p.placed_end_ms) for p in placed]
    mixed_wav, bgm_wav = cfg.workdir / "mixed_audio.wav", cfg.workdir / "original_audio.wav"

    mixed: AudioSegment | None = None
    if media.has_audio and cfg.keep_bgm:
        log.info("Video gốc có âm thanh → tách BGM và ducking (BGM còn %.0f%% khi có giọng đọc).", cfg.bgm_duck_ratio * 100)
        run_cmd(["ffmpeg", "-y", "-loglevel", "error", "-i", media.path, "-vn", "-map", "0:a:0", "-ac", "2",
                 "-ar", MIX_SAMPLE_RATE, "-c:a", "pcm_s16le", bgm_wav], "tách audio gốc", timeout=1800)
        bgm = AudioSegment.from_wav(bgm_wav)
        if bgm.dBFS < SILENT_BGM_DBFS:
            log.info("Âm thanh gốc gần như im lặng → bỏ qua ducking.")
        else:
            mixed = duck_and_mix(bgm, voice_track, ranges, total_ms, cfg)
    elif media.has_audio:
        log.info("Bỏ âm thanh gốc (--no-bgm): chỉ dùng giọng thuyết minh.")
    if mixed is None:
        silent = AudioSegment.silent(duration=total_ms, frame_rate=MIX_SAMPLE_RATE).set_channels(2)
        mixed = duck_and_mix(silent, voice_track, [], total_ms, cfg)
    mixed.export(mixed_wav, format="wav")

    _final_mux(cfg, media.path, mixed_wav)
    if not cfg.keep_temp:
        for f in (mixed_wav, bgm_wav):
            f.unlink(missing_ok=True)
    log.info("Hoàn tất → %s", cfg.final_video_path)


# ════════════════════════════════════════════════════════════
# Ducking + mux theo STREAM/TỪNG CHUNK (dùng cho video dài — xem long_video_pipeline.py)
# ════════════════════════════════════════════════════════════
# `mux_video()` ở trên tách NGUYÊN BGM của cả video ra rồi giữ trong RAM dưới dạng mảng numpy để duck+mix
# (float32, stereo, 44.1kHz ⇒ ~635 MB/giờ) — chấp nhận được với video ngắn/vừa nhưng KHÔNG PHÙ HỢP với
# video hàng giờ. `mux_video_streaming()` dưới đây làm đúng việc tương tự nhưng CHỈ XỬ LÝ TỪNG MACRO-CHUNK
# một lúc (BGM của chunk được trích riêng bằng FFmpeg, duck+mix trong RAM chỉ với ~vài chục MB của CHUNK
# ĐÓ, rồi giải phóng), sau đó NỐI các đoạn WAV đã mix của từng chunk lại bằng FFmpeg **concat demuxer**
# (`-f concat -c copy`) — đây chính là kỹ thuật mà VideoLingo/pyvideotrans dùng để nối nhiều file audio/
# video mà không phải chạy `-filter_complex` với hàng chục input cùng lúc (nguyên nhân gây WinError 206
# trên Windows khi câu lệnh quá dài): concat demuxer chỉ đọc một file danh sách văn bản, mỗi dòng một
# đường dẫn, không giới hạn số lượng theo độ dài command-line.
def _extract_bgm_slice(cfg: Config, media: MediaInfo, start_sec: float, end_sec: float, out_wav: Path) -> None:
    run_cmd(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start_sec:.3f}", "-to", f"{end_sec:.3f}",
             "-i", media.path, "-vn", "-map", "0:a:0", "-ac", "2", "-ar", MIX_SAMPLE_RATE,
             "-c:a", "pcm_s16le", out_wav], f"tách BGM [{start_sec:.1f}s–{end_sec:.1f}s]", timeout=600)


def mix_one_chunk(cfg: Config, media: MediaInfo, chunk_start_sec: float, chunk_end_sec: float,
                  voice_track: AudioSegment, ranges_local_ms: list[tuple[int, int]], out_wav: Path,
                  bgm_scratch_wav: Path) -> None:
    """Duck + mix cho ĐÚNG MỘT macro-chunk. `voice_track` và `ranges_local_ms` đã ở mốc thời gian TƯƠNG ĐỐI
    so với đầu chunk (0 = chunk_start_sec) — độc lập với phần còn lại của video, nên hàm này chỉ cần đúng
    dữ liệu của chunk hiện tại trong RAM, không quan tâm video dài bao nhiêu."""
    chunk_ms = int(round((chunk_end_sec - chunk_start_sec) * 1000))
    mixed: AudioSegment | None = None
    if media.has_audio and cfg.keep_bgm:
        _extract_bgm_slice(cfg, media, chunk_start_sec, chunk_end_sec, bgm_scratch_wav)
        bgm = AudioSegment.from_wav(bgm_scratch_wav)
        if bgm.dBFS >= SILENT_BGM_DBFS:
            mixed = duck_and_mix(bgm, voice_track, ranges_local_ms, chunk_ms, cfg)
    if mixed is None:
        silent = AudioSegment.silent(duration=chunk_ms, frame_rate=MIX_SAMPLE_RATE).set_channels(2)
        mixed = duck_and_mix(silent, voice_track, [] if not (media.has_audio and cfg.keep_bgm) else ranges_local_ms,
                             chunk_ms, cfg)
    mixed.export(out_wav, format="wav")
    del mixed                                            # giải phóng mảng audio của chunk ngay khi đã ghi ra đĩa


def concat_wavs(wav_paths: list[Path], out_wav: Path, workdir: Path) -> None:
    """Nối nhiều file WAV (cùng định dạng) bằng FFmpeg concat demuxer — KHÔNG re-encode (`-c copy`), KHÔNG
    load bất kỳ file nào vào RAM ở phía Python: FFmpeg tự đọc/ghi theo dòng chảy (stream) giữa các file."""
    if not wav_paths:
        raise PipelineError("Không có đoạn audio nào để nối.")
    if len(wav_paths) == 1:
        wav_paths[0].replace(out_wav) if wav_paths[0] != out_wav else None
        return
    list_file = workdir / "concat_list.txt"
    lines = ["file '" + str(p.resolve()).replace("'", "'\\''") + "'" for p in wav_paths]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_cmd(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out_wav],
            f"nối {len(wav_paths)} đoạn audio (concat demuxer)", timeout=1800)
    list_file.unlink(missing_ok=True)


def mux_video_streaming(cfg: Config, media: MediaInfo, chunk_windows: list[tuple[float, float]],
                        chunk_voice_paths: list[Path]) -> None:
    """Ducking + mux cho video dài, xử lý TỪNG CHUNK một, bộ nhớ không phụ thuộc độ dài video.

    `chunk_windows[i]` = (start_sec, end_sec) của macro-chunk thứ i (từ chunk_planner.plan_macro_chunks).
    `chunk_voice_paths[i]` = file audio giọng đọc ĐÃ GHÉP SẴN cho đúng chunk đó (từ audio_assembler,
    thời lượng = end_sec - start_sec), do long_video_pipeline.py tạo ra ở bước 04_assembled của mỗi chunk.
    """
    if len(chunk_windows) != len(chunk_voice_paths):
        raise PipelineError("Số chunk và số file voice không khớp.")
    scratch_dir = cfg.workdir / "mux_chunks"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    mixed_paths: list[Path] = []
    try:
        for i, (start, end) in enumerate(chunk_windows):
            log.info("Ducking + mix chunk %d/%d (%.1fs–%.1fs)...", i + 1, len(chunk_windows), start, end)
            voice = AudioSegment.from_file(chunk_voice_paths[i])
            out_wav = scratch_dir / f"mixed_{i:04d}.wav"
            mix_one_chunk(cfg, media, start, end, voice, _voice_ranges(voice), out_wav, scratch_dir / f"bgm_{i:04d}.wav")
            mixed_paths.append(out_wav)
            del voice
        final_wav = cfg.workdir / "mixed_audio.wav"
        concat_wavs(mixed_paths, final_wav, scratch_dir)
        _final_mux(cfg, media.path, final_wav)
    finally:
        if not cfg.keep_temp:
            for f in scratch_dir.glob("*"):
                f.unlink(missing_ok=True)
            for f in [cfg.workdir / "mixed_audio.wav"]:
                f.unlink(missing_ok=True)
    log.info("Hoàn tất (streaming, %d chunk) → %s", len(chunk_windows), cfg.final_video_path)


def _voice_ranges(voice: AudioSegment, silence_thresh_dbfs: float = -45.0, min_gap_ms: int = 250) -> list[tuple[int, int]]:
    """Suy ra các khoảng "có giọng đọc" (để duck BGM) trực tiếp từ chính file voice track của chunk, thay vì
    phải truyền riêng danh sách segment — đơn giản hoá giao diện `mux_video_streaming()`. Dùng phát hiện
    khoảng lặng của pydub trên một file NHỎ (đã là audio của 1 chunk, vài phút) nên không tốn kém."""
    from pydub.silence import detect_nonsilent
    return detect_nonsilent(voice, min_silence_len=min_gap_ms, silence_thresh=silence_thresh_dbfs)


def _final_mux(cfg: Config, video: Path, audio: Path) -> None:
    """Gọi FFmpeg TRỰC TIẾP qua `utils.run_cmd` (không qua thư viện `ffmpeg-python`): thư viện đó gọi thẳng
    `subprocess.Popen` nội bộ, không cho truyền `creationflags` để ẩn cửa sổ console trên Windows — đây
    chính là bước tạo ra file video CUỐI CÙNG nên bắt buộc phải đi qua `run_cmd` như mọi lệnh FFmpeg khác
    trong dự án (xem `utils._subprocess_kwargs`)."""
    out = cfg.final_video_path
    out.parent.mkdir(parents=True, exist_ok=True)

    def run(video_opts: list[str]) -> None:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0",
               *video_opts, "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-movflags", "+faststart", "-shortest", out]
        run_cmd(cmd, "mux video cuối", timeout=3600)

    try:
        log.info("FFmpeg mux (copy video, không encode lại)...")
        run(["-c:v", "copy"])
    except MediaToolError as e:
        log.warning("Không thể copy luồng video vào MP4 (%s) → mã hoá lại bằng libx264.", str(e).splitlines()[-1])
        run(["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"])
    if not out.is_file() or out.stat().st_size == 0:
        raise PipelineError("FFmpeg không tạo được file video đầu ra.")
