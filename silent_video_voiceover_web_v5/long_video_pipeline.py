"""Orchestrator DUY NHẤT của pipeline — thay thế `main.run_pipeline()` cũ.

Video NGẮN cũng được xử lý qua đây: `chunk_planner.plan_macro_chunks()` trả về đúng 1 chunk bao trọn video
khi video ngắn hơn `chunk_target_sec` — nên video ngắn chỉ là trường hợp đặc biệt N=1 của cùng một pipeline,
không cần hai bộ code riêng. Điều này cũng có nghĩa: MỌI cải tiến (checkpoint/resume, streaming mux,
tách engine...) áp dụng cho cả video 30 giây lẫn video 3 giờ.

Luồng xử lý cho mỗi macro-chunk (độc lập, checkpoint riêng — xem checkpoint.py):
    01_extracted → 02_script → 03_tts_done → 04_assembled
Sau khi TẤT CẢ chunk xong bước 04 (dù ở lần chạy này hay các lần chạy trước, nhờ resume), pipeline mới
ghép các đoạn voiceover của từng chunk lại (FFmpeg concat demuxer, xem video_muxer.mux_video_streaming)
và mux vào video gốc.

Quản lý bộ nhớ: RAM/VRAM dùng cho vision+TTS+audio tại một thời điểm chỉ tỉ lệ với KÍCH THƯỚC 1 CHUNK
(mặc định ~4 phút video), KHÔNG tỉ lệ với độ dài toàn bộ video. Sau mỗi chunk, các biến giữ frame/audio của
chunk đó ra khỏi phạm vi (scope) và `gc.collect()` được gọi để chủ động giải phóng ngay, thay vì đợi Python
tự dọn — quan trọng với chuỗi 15-20 chunk chạy liên tục trong cùng một tiến trình.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import audio_assembler
import script_generator
import tts_engine
import video_muxer
import video_processor
from checkpoint import PipelineCheckpoint, Stage
from chunk_planner import MacroChunk, plan_macro_chunks
from config import Config
from engine_factory import get_tts_engine, open_vision_engine
from engines.base import BaseVisionEngine
from script_generator import ScriptSegment
from silence_detector import detect_silences
from tts_engine import TTSResult
from utils import MediaInfo, PipelineError, banner, check_binaries, probe_media, read_json, write_json
from video_processor import FrameSample

log = logging.getLogger("svvc.long_video")

CHUNK_STAGES = 4    # 01_extracted, 02_script, 03_tts_done, 04_assembled (mỗi chunk đi qua đủ 4 giai đoạn này)
PIPELINE_STEPS = 4  # các bước cấp cao hiển thị trong banner(): chia chunk, kịch bản, TTS, ghép+mux


# ════════════════════════════════════════════════════════════
# (De)serialize — mỗi checkpoint là JSON, nên dataclass ↔ dict tại ranh giới này
# ════════════════════════════════════════════════════════════
def _frame_to_dict(s: FrameSample) -> dict:
    return {"index": s.index, "timestamp_sec": s.timestamp_sec, "path": str(s.path),
            "window_start": s.window_start, "window_end": s.window_end}


def _frame_from_dict(d: dict) -> FrameSample:
    return FrameSample(index=d["index"], timestamp_sec=d["timestamp_sec"], path=Path(d["path"]),
                       window_start=d["window_start"], window_end=d["window_end"])


def _segment_to_dict(s: ScriptSegment) -> dict:
    return {"id": s.id, "start_sec": s.start_sec, "end_sec": s.end_sec, "text": s.text, "tone": s.tone}


def _segment_from_dict(d: dict) -> ScriptSegment:
    return ScriptSegment(id=d["id"], start_sec=d["start_sec"], end_sec=d["end_sec"], text=d["text"], tone=d["tone"])


def _shift_segment(s: ScriptSegment, delta: float) -> ScriptSegment:
    """Dịch một đoạn kịch bản theo `delta` giây — dùng để chuyển từ mốc TUYỆT ĐỐI (toàn video, cái người
    dùng nhìn thấy trong script.json) sang mốc TƯƠNG ĐỐI so với đầu chunk (cái TTS/assembler cần để xử lý
    một chunk độc lập với phần còn lại của video), và ngược lại."""
    return ScriptSegment(id=s.id, start_sec=s.start_sec + delta, end_sec=s.end_sec + delta, text=s.text, tone=s.tone)


def _tts_result_to_dict(r: TTSResult) -> dict:
    return r.to_dict()


def _tts_result_from_dict(d: dict) -> TTSResult:
    r = TTSResult(id=d["id"], text=d["text"], start_sec=d["start_sec"], planned_sec=d["planned_sec"],
                 available_sec=d["available_sec"], raw_sec=d.get("raw_sec", 0.0), final_sec=d.get("final_sec", 0.0),
                 ratio=d.get("ratio", 1.0), speed=d.get("speed", 1.0), status=d.get("status", "pending"),
                 error=d.get("error"))
    r.raw_path = Path(d["raw_path"]) if d.get("raw_path") else None
    r.fitted_path = Path(d["fitted_path"]) if d.get("fitted_path") else None
    return r


def _chunk_to_dict(c: MacroChunk) -> dict:
    return {"index": c.index, "start_sec": c.start_sec, "end_sec": c.end_sec, "cut_reason": c.cut_reason}


def _chunk_from_dict(d: dict) -> MacroChunk:
    return MacroChunk(index=d["index"], start_sec=d["start_sec"], end_sec=d["end_sec"], cut_reason=d["cut_reason"])


# ════════════════════════════════════════════════════════════
# Fingerprint — mọi tham số ảnh hưởng tới KẾT QUẢ (không phải hiệu năng) phải có mặt ở đây,
# để đổi tham số nào trong số này thì checkpoint cũ tự động không được dùng nhầm nữa.
# ════════════════════════════════════════════════════════════
def build_fingerprint(cfg: Config, media: MediaInfo) -> dict:
    stat = cfg.video_path.stat()
    return {
        "video_name": cfg.video_path.name, "video_size": stat.st_size, "video_mtime": int(stat.st_mtime),
        "duration": round(media.duration_sec, 2),
        "engine": cfg.engine, "gemini_model": cfg.gemini_model if cfg.engine == "api" else None,
        "tts_engine": cfg.tts_engine, "voice": cfg.voice,
        "language": cfg.language, "style": cfg.style, "words_per_sec": cfg.words_per_sec,
        "wps_tolerance": cfg.wps_tolerance, "min_segment_sec": cfg.min_segment_sec,
        "extract_mode": cfg.extract_mode, "frame_interval_sec": cfg.frame_interval_sec,
        "scene_threshold": cfg.scene_threshold, "frames_per_prompt": cfg.frames_per_prompt,
        "frame_max_width": cfg.frame_max_width, "jpeg_quality": cfg.jpeg_quality,
        "chunk_target_sec": cfg.chunk_target_sec, "chunk_tolerance_sec": cfg.chunk_tolerance_sec,
        "min_chunk_sec": cfg.min_chunk_sec, "silence_min_sec": cfg.silence_min_sec,
        "silence_noise_db": cfg.silence_noise_db,
        "max_speedup": cfg.max_speedup, "allow_video_retime": cfg.allow_video_retime,
        "bgm_duck_ratio": cfg.bgm_duck_ratio, "keep_bgm": cfg.keep_bgm, "voice_gain_db": cfg.voice_gain_db,
    }


# ════════════════════════════════════════════════════════════
# Bước 0: phân tích TOÀN VIDEO một lần (silence + scene cuts) → chia macro-chunk
# ════════════════════════════════════════════════════════════
def _analysis_path(checkpoint: PipelineCheckpoint) -> Path:
    return checkpoint.dir / "00_analysis.json"     # nằm trong thư mục theo fingerprint → tự invalidate đúng lúc


def _analyze_full_video(cfg: Config, media: MediaInfo, checkpoint: PipelineCheckpoint) -> tuple[list[tuple[float, float]], list[float]]:
    path = _analysis_path(checkpoint)
    if path.is_file():
        try:
            data = read_json(path)
            log.info("Dùng lại kết quả quét khoảng lặng/scene-cut đã lưu (thêm --force để quét lại).")
            return [tuple(x) for x in data["silences"]], list(data["scene_cuts"])
        except (PipelineError, KeyError, TypeError):
            log.warning("Cache phân tích video hỏng → quét lại.")

    silences = detect_silences(cfg.video_path, has_audio=media.has_audio, min_silence_sec=cfg.silence_min_sec,
                               noise_db=cfg.silence_noise_db)
    scene_cuts: list[float] = []
    if cfg.extract_mode == "scene":
        scene_cuts = video_processor.detect_scene_cuts(cfg.video_path, media, cfg.scene_threshold, cfg.min_scene_sec)
        log.info("OpenCV phát hiện %d cảnh cắt trên toàn video.", len(scene_cuts))
    write_json(path, {"silences": silences, "scene_cuts": scene_cuts})
    return silences, scene_cuts


def _load_or_plan_chunks(cfg: Config, media: MediaInfo, checkpoint: PipelineCheckpoint) -> tuple[list[MacroChunk], list[float]]:
    silences, scene_cuts = _analyze_full_video(cfg, media, checkpoint)
    cached = checkpoint.load_plan()
    if cached is not None:
        chunks = [_chunk_from_dict(d) for d in cached]
        log.info("Dùng lại kế hoạch chia %d chunk đã lưu (thêm --force để chia lại).", len(chunks))
        return chunks, scene_cuts

    chunks = plan_macro_chunks(media.duration_sec, target_sec=cfg.chunk_target_sec, tolerance_sec=cfg.chunk_tolerance_sec,
                               min_chunk_sec=cfg.min_chunk_sec, silences=silences, scene_cuts=scene_cuts)
    checkpoint.save_plan([_chunk_to_dict(c) for c in chunks])
    return chunks, scene_cuts


# ════════════════════════════════════════════════════════════
# Xử lý MỘT macro-chunk — TÁCH THÀNH HAI PHA để giữ đúng trải nghiệm của bản gốc:
#   Pha A (script): mọi chunk đi qua 01_extracted → 02_script trước, RỒI MỚI tổng hợp script.json
#                   cho TOÀN VIDEO — cho phép `--stop-after 2` dừng lại để người dùng duyệt/sửa tay,
#                   giống hệt pipeline video ngắn ban đầu, chỉ khác là giờ áp dụng cho N chunk.
#   Pha B (tts+assemble): 03_tts_done → 04_assembled cho từng chunk.
# Tách hai pha KHÔNG làm mất tính "xử lý từng chunk độc lập, bộ nhớ hằng số": Pha A chỉ giữ đường dẫn ảnh
# (rất nhẹ) qua các chunk, không giữ pixel; RAM nặng (audio) chỉ phát sinh trong Pha B, vẫn theo từng chunk.
# ════════════════════════════════════════════════════════════
def _ensure_script(cfg: Config, media: MediaInfo, chunk: MacroChunk, checkpoint: PipelineCheckpoint,
                   vision: BaseVisionEngine, scene_cuts: list[float], story_hook: str | None
                   ) -> tuple[list[ScriptSegment], str | None]:
    samples: list[FrameSample] | None = None
    if checkpoint.has(chunk.index, Stage.EXTRACTED):
        cand = [_frame_from_dict(d) for d in checkpoint.load(chunk.index, Stage.EXTRACTED)["samples"]]
        if all(s.path.is_file() for s in cand):
            samples = cand
        else:
            log.warning("Chunk #%d: frame đã lưu bị mất trên đĩa (work/ có thể đã bị dọn) → trích lại.", chunk.index)
    if samples is None:
        samples = video_processor.extract_samples_in_range(cfg, media, chunk.index, chunk.start_sec, chunk.end_sec,
                                                            scene_cuts_full_video=scene_cuts)
        checkpoint.save(chunk.index, Stage.EXTRACTED, {"samples": [_frame_to_dict(s) for s in samples]})

    if checkpoint.has(chunk.index, Stage.SCRIPT):
        segments = [_segment_from_dict(d) for d in checkpoint.load(chunk.index, Stage.SCRIPT)["segments"]]
    else:
        segments, story_hook = script_generator.generate_script(
            vision, samples, cfg, media, batch_tag=f"chunk{chunk.index:04d}", context_duration_sec=media.duration_sec,
            is_first_chunk=(chunk.index == 0), story_hook=story_hook)
        segments = script_generator.validate_and_fix(segments, cfg, chunk.end_sec, vision, auto_shorten=True,
                                                      min_time=chunk.start_sec)
        checkpoint.save(chunk.index, Stage.SCRIPT, {"segments": [_segment_to_dict(s) for s in segments]})
    del samples
    gc.collect()
    return segments, story_hook


def _ensure_tts_and_assemble(cfg: Config, chunk: MacroChunk, checkpoint: PipelineCheckpoint,
                             segments: list[ScriptSegment], tts: tts_engine.BaseTTSEngine) -> Path:
    tag = f"chunk{chunk.index:04d}"
    if checkpoint.has(chunk.index, Stage.TTS):
        results = [_tts_result_from_dict(d) for d in checkpoint.load(chunk.index, Stage.TTS)["results"]]
    else:
        local_segments = [_shift_segment(s, -chunk.start_sec) for s in segments]     # 0-based trong phạm vi chunk
        results = asyncio.run(tts_engine.synthesize_all(local_segments, cfg, chunk.duration_sec, tts_engine=tts))
        checkpoint.save(chunk.index, Stage.TTS, {"results": [_tts_result_to_dict(r) for r in results]})
    gc.collect()

    if checkpoint.has(chunk.index, Stage.ASSEMBLED):
        audio_path = Path(checkpoint.load(chunk.index, Stage.ASSEMBLED)["audio_path"])
    else:
        cfg.chunk_audio_dir.mkdir(parents=True, exist_ok=True)
        cfg.chunk_reports_dir.mkdir(parents=True, exist_ok=True)
        audio_path = cfg.chunk_audio_dir / f"{tag}.mp3"
        report_path = cfg.chunk_reports_dir / f"{tag}.json"
        chunk_ms = int(round(chunk.duration_sec * 1000))
        track, placed = audio_assembler.assemble_voiceover_track(results, chunk_ms, cfg, out_path=audio_path,
                                                                  report_path=report_path)
        del track, placed                              # đoạn audio đã ghi ra đĩa, không cần giữ mảng PCM trong RAM nữa
        checkpoint.save(chunk.index, Stage.ASSEMBLED, {"audio_path": str(audio_path), "report_path": str(report_path)})
    gc.collect()
    return audio_path


def _distribute_segments_to_chunks(all_segments: list[ScriptSegment], chunks: list[MacroChunk],
                                   checkpoint: PipelineCheckpoint, cfg: Config, *, source_label: str
                                   ) -> list[list[ScriptSegment]]:
    """Dùng chung cho MỌI trường hợp đã có sẵn kịch bản ĐẦY ĐỦ cho toàn video (mốc thời gian TUYỆT ĐỐI) mà
    KHÔNG cần gọi Gemini theo từng chunk nữa — hiện có 2 nguồn: `--script-file` (người dùng tự viết) và
    kịch bản lấy qua link YouTube (`youtube_source.py`). Chia đúng theo `chunk.start_sec/end_sec`, kiểm
    tra/chuẩn hoá bằng `validate_and_fix`, rồi checkpoint THẲNG ở cả 2 giai đoạn EXTRACTED+SCRIPT — bỏ qua
    hoàn toàn việc trích frame cục bộ và gọi Gemini."""
    by_chunk: list[list[ScriptSegment]] = [[] for _ in chunks]
    for s in all_segments:
        mid = (s.start_sec + s.end_sec) / 2
        idx = next((c.index for c in chunks if c.start_sec <= mid < c.end_sec), chunks[-1].index)
        by_chunk[idx].append(s)

    result: list[list[ScriptSegment]] = []
    for chunk, segs in zip(chunks, by_chunk):
        if not segs:
            log.warning("Chunk #%d (%.0fs–%.0fs) không có đoạn nào từ %s.", chunk.index, chunk.start_sec,
                        chunk.end_sec, source_label)
            fixed: list[ScriptSegment] = []
        else:
            fixed = script_generator.validate_and_fix(segs, cfg, chunk.end_sec, None, auto_shorten=False,
                                                       min_time=chunk.start_sec)
        checkpoint.save(chunk.index, Stage.EXTRACTED, {"samples": [], "source": source_label})
        checkpoint.save(chunk.index, Stage.SCRIPT, {"segments": [_segment_to_dict(s) for s in fixed]})
        result.append(fixed)
    log.info("Đã phân phối kịch bản từ %s (%d đoạn) cho %d chunk, bỏ qua bước trích frame/gọi Gemini theo chunk.",
             source_label, len(all_segments), len(chunks))
    return result


def _import_script_file(cfg: Config, chunks: list[MacroChunk], checkpoint: PipelineCheckpoint
                        ) -> list[list[ScriptSegment]]:
    """`--script-file`: nạp kịch bản người dùng tự viết/sửa tay (mốc thời gian TUYỆT ĐỐI, toàn video)."""
    all_segments = script_generator.load_script_file(cfg.script_file)
    return _distribute_segments_to_chunks(all_segments, chunks, checkpoint, cfg, source_label=cfg.script_file.name)


# ════════════════════════════════════════════════════════════
# Gộp kết quả cuối: script.json + sync_report.json cho toàn video (để người dùng xem/sửa như trước)
# ════════════════════════════════════════════════════════════
def _save_aggregate_script(cfg: Config, segments_by_chunk: list[list[ScriptSegment]]) -> None:
    out = []
    next_id = 1
    for chunk_segments in segments_by_chunk:
        for s in sorted(chunk_segments, key=lambda x: x.start_sec):
            d = s.to_json()
            d["id"] = next_id
            next_id += 1
            out.append(d)
    write_json(cfg.script_path, out)
    log.info("Đã gộp kịch bản của %d chunk → %s (%d đoạn).", len(segments_by_chunk), cfg.script_path, len(out))


def _aggregate_sync_report(cfg: Config, chunks: list[MacroChunk]) -> None:
    segments, summary = [], {"segments": 0, "start_drift_over_tolerance": 0, "overruns": 0, "clipped": 0, "overlaps": 0}
    for c in chunks:
        p = cfg.chunk_reports_dir / f"chunk{c.index:04d}.json"
        if not p.is_file():
            continue
        data = read_json(p)
        for k in summary:
            summary[k] += data["summary"].get(k, 0)
        for seg in data["segments"]:                    # đưa mốc thời gian của chunk về ABSOLUTE (toàn video)
            seg = dict(seg)
            offset_ms = int(round(c.start_sec * 1000))
            for field in ("planned_start_ms", "planned_end_ms", "placed_start_ms", "placed_end_ms"):
                seg[field] += offset_ms
            segments.append(seg)
    write_json(cfg.sync_report_path, {"total_video_ms": int(round(chunks[-1].end_sec * 1000)) if chunks else 0,
                                      "chunks": len(chunks), "summary": summary, "segments": segments})
    if summary["start_drift_over_tolerance"] or summary["clipped"]:
        log.warning("Tổng hợp toàn video: %d đoạn lệch nhịp, %d đoạn bị cắt bớt — xem chi tiết trong %s.",
                    summary["start_drift_over_tolerance"], summary["clipped"], cfg.sync_report_path.name)
    else:
        log.info("Tổng hợp toàn video: %d đoạn, không lệch nhịp/không bị cắt.", summary["segments"])


def _resolve_youtube_source(cfg: Config, *, interactive: bool | None) -> tuple[Path, list[ScriptSegment]]:
    """Chạy SONG SONG 2 việc độc lập (xem giải thích kiến trúc trong `youtube_source.py`):
      • Luồng nền: tải video thật về máy bằng `yt-dlp` — cần cho các bước sau (TTS/ghép/mux).
      • Luồng chính: gửi NGUYÊN VĂN link cho Gemini Web, yêu cầu viết kịch bản cho TOÀN BỘ video.
    An toàn chạy song song: việc tải dùng `yt-dlp` (không đụng Playwright); việc hỏi Gemini dùng Playwright
    NHƯNG chỉ ở luồng chính — không có 2 luồng nào cùng thao tác trên 1 trình duyệt cùng lúc.
    Trả về (đường dẫn video đã tải, danh sách ScriptSegment đã bóc tách — CHƯA qua validate_and_fix, việc
    đó xảy ra sau khi đã biết ranh giới từng macro-chunk, xem `_distribute_segments_to_chunks`)."""
    import youtube_source
    download_path = cfg.workdir / "youtube_source.mp4"
    download_error: list[Exception] = []

    def _bg_download() -> None:
        try:
            youtube_source.download_youtube_video(cfg.youtube_url, download_path)
        except Exception as e:  # noqa: BLE001 — bắt lại để báo lỗi rõ ràng ở luồng chính, không làm crash luồng nền âm thầm
            download_error.append(e)

    t = threading.Thread(target=_bg_download, daemon=True, name="youtube-download")
    t.start()
    log.info("Đang tải video (chạy nền) VÀ gửi link cho Gemini Web (song song) — không đợi tải xong mới bắt đầu hỏi Gemini.")

    with open_vision_engine(cfg, interactive=interactive) as vision:
        raw = youtube_source.analyze_youtube_video(vision, cfg, cfg.youtube_url)

    t.join(timeout=1800)
    if t.is_alive():
        raise PipelineError("Tải video YouTube quá lâu (>30 phút) — video có thể quá dài hoặc mạng quá chậm.")
    if download_error:
        raise download_error[0]

    segments: list[ScriptSegment] = []
    for item in raw:
        seg = script_generator._coerce_segment(item)  # noqa: SLF001 — dùng lại nguyên bộ parse chung với mọi nguồn kịch bản khác
        if seg is not None:
            segments.append(seg)
    if not segments:
        raise PipelineError("Gemini không trả về đoạn thuyết minh hợp lệ nào từ link YouTube. Thử lại, hoặc "
                            "chạy --check-web / bật chế độ headed để xem Gemini thực sự phản hồi gì.")
    log.info("Đã nhận %d đoạn thuyết minh từ Gemini (qua link YouTube, chưa kiểm tra/rút gọn).", len(segments))
    return download_path, segments


# ════════════════════════════════════════════════════════════
# Điểm vào
# ════════════════════════════════════════════════════════════
def run(cfg: Config, *, on_chunk_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[int, str], None] | None = None, interactive: bool | None = None
        ) -> dict[str, Path | None]:
    """`interactive`: có cho phép hỏi lại người dùng qua `input()` khi phiên Gemini Web hết hạn hay không.
    Mặc định (`None`) tự dò qua `sys.stdin` — đúng khi chạy CLI thật. Giao diện GUI (app_streamlit.py) LUÔN
    truyền `interactive=False` tường minh, vì nó có luồng đăng nhập RIÊNG bằng nút bấm, không được gọi
    `input()` (sẽ treo/lỗi vì không có console để nhập)."""
    youtube_segments: list[ScriptSegment] | None = None
    if cfg.youtube_url:
        # LƯU Ý: KHÔNG kiểm tra "video_path vừa có vừa có youtube_url" ở đây — việc đó đã kiểm tra MỘT LẦN
        # lúc khởi tạo Config (config.py __post_init__). Nếu kiểm tra lại ở đây sẽ báo nhầm lỗi khi cfg được
        # TÁI SỬ DỤNG cho lần chạy resume thứ 2 trở đi, vì run() đã tự gán cfg.video_path ở lần chạy đầu.
        cfg.ensure_dirs()
        cached_video = cfg.workdir / "youtube_source.mp4"
        if cached_video.is_file() and not cfg.force:
            log.info("Video từ link YouTube đã tải sẵn ở lần chạy trước → dùng lại, KHÔNG tải lại/gọi Gemini "
                     "lại: %s (dùng --force nếu muốn lấy lại từ đầu).", cached_video.name)
            cfg.video_path = cached_video
        else:
            banner(0, PIPELINE_STEPS, "Nhập kịch bản từ link YouTube + tải video (chạy song song)")
            if on_step:
                on_step(0, "Đọc link YouTube")
            cfg.video_path, youtube_segments = _resolve_youtube_source(cfg, interactive=interactive)

    if cfg.video_path is None:
        raise PipelineError("Thiếu video đầu vào.")
    t0 = time.perf_counter()
    cfg.ensure_dirs()
    check_binaries()
    media = probe_media(cfg.video_path)
    log.info("Video: %s | %.1fs | %dx%d @ %.2f fps | âm thanh gốc: %s | engine: %s/%s", cfg.video_path.name,
             media.duration_sec, media.width, media.height, media.fps, "có" if media.has_audio else "không",
             cfg.engine, cfg.tts_engine)

    # ── GIAI ĐOẠN 0 (tuỳ chọn): chỉ giữ cảnh hay — chạy TRƯỚC MỌI THỨ khác để không lãng phí tài nguyên
    # phân tích/TTS cho phần video sẽ bị cắt bỏ. Sau bước này, `cfg.video_path`/`media` được THAY THẾ bằng
    # video đã cắt — toàn bộ phần còn lại của pipeline (chunk, kịch bản, TTS, ghép) không biết và không cần
    # biết có highlight hay không, cứ xử lý bình thường trên video (đã có thể ngắn hơn) này.
    if cfg.highlight_mode:
        banner(0, PIPELINE_STEPS, "Chỉ giữ cảnh hay (highlight) — quét nhanh toàn video trước khi xử lý chi tiết")
        if on_step:
            on_step(0, "Chọn cảnh hay")
        import highlight_selector
        trimmed = highlight_selector.get_or_build_highlight_video(cfg, media, interactive=interactive)
        if trimmed != cfg.video_path:
            cfg.video_path = trimmed
            media = probe_media(cfg.video_path)
            log.info("Từ đây, toàn bộ pipeline xử lý trên video ĐÃ CẮT: %s (%.1fs).", trimmed.name, media.duration_sec)

    fp = build_fingerprint(cfg, media)
    checkpoint = PipelineCheckpoint(cfg.workdir, fp, force=cfg.force)

    banner(1, PIPELINE_STEPS, "Phân đoạn động (silence/scene detection) & lập kế hoạch chunk")
    if on_step:
        on_step(1, "Phân đoạn động")
    chunks, scene_cuts = _load_or_plan_chunks(cfg, media, checkpoint)
    is_long = len(chunks) > 1
    log.info("%s %s", f"Video DÀI: chia thành {len(chunks)} macro-chunk." if is_long else
             "Video ngắn hơn ngưỡng chia chunk → xử lý như 1 chunk duy nhất.", checkpoint.resume_summary(len(chunks)))

    outputs: dict[str, Path | None] = {"script": None, "voiceover": None, "video": None}
    if cfg.stop_after == 0:                              # chỉ muốn xem kế hoạch chia chunk, chưa gọi Gemini/TTS
        return outputs
    for c in chunks:
        if cfg.force_chunk == c.index:
            log.info("--force-chunk %d: bỏ qua checkpoint của riêng chunk này, làm lại từ đầu.", c.index)
            checkpoint.invalidate_from(c.index, Stage.EXTRACTED)

    # ── Pha A: kịch bản (script) cho TỪNG chunk ───────────────────────────────
    banner(2, PIPELINE_STEPS, "Trích frame & tạo kịch bản (script) cho từng chunk")
    if on_step:
        on_step(2, "Tạo kịch bản")
    if cfg.script_file:
        segments_by_chunk = _import_script_file(cfg, chunks, checkpoint)
    elif youtube_segments is not None:
        segments_by_chunk = _distribute_segments_to_chunks(youtube_segments, chunks, checkpoint, cfg,
                                                            source_label=f"link YouTube ({cfg.youtube_url})")
    else:
        with open_vision_engine(cfg, interactive=interactive) as vision:
            segments_by_chunk = []
            story_hook: str | None = None   # "fantasy_inspiring": câu chuyện mở đầu, mang xuyên suốt mọi chunk
            for chunk in chunks:
                if checkpoint.has(chunk.index, Stage.SCRIPT):
                    log.info("Chunk #%d/%d: kịch bản đã có (RESUME) → bỏ qua.", chunk.index + 1, len(chunks))
                    segs = [_segment_from_dict(d) for d in checkpoint.load(chunk.index, Stage.SCRIPT)["segments"]]
                    if chunk.index == 0 and cfg.narrative_style == "fantasy_inspiring" and story_hook is None and segs:
                        # kịch bản chunk 0 lấy lại từ lần chạy TRƯỚC (chưa gọi generate_script lần này) →
                        # tự trích câu chuyện mở đầu từ nội dung đã lưu, để các chunk sau vẫn nhắc lại được
                        hook_text = " ".join(s.text for s in segs[:2])
                        story_hook = (hook_text[:280] + "…") if len(hook_text) > 280 else hook_text
                else:
                    log.info("Chunk #%d/%d (%.0fs–%.0fs, cắt tại: %s)...", chunk.index + 1, len(chunks),
                             chunk.start_sec, chunk.end_sec, chunk.cut_reason)
                    segs, story_hook = _ensure_script(cfg, media, chunk, checkpoint, vision, scene_cuts, story_hook)
                segments_by_chunk.append(segs)
                if on_chunk_progress:
                    on_chunk_progress(chunk.index + 1, len(chunks))
                gc.collect()

    outputs["script"] = cfg.script_path
    _save_aggregate_script(cfg, segments_by_chunk)
    if cfg.stop_after == 2:
        log.info("Dừng sau khi tạo kịch bản theo yêu cầu (--stop-after 2). Sửa xong, chạy lại đúng lệnh này để "
                "tiếp tục — các chunk đã có kịch bản sẽ không gọi lại Gemini. Kịch bản: %s", cfg.script_path)
        return outputs

    # ── Pha B: TTS + ghép audio cho TỪNG chunk ────────────────────────────────
    banner(3, PIPELINE_STEPS, f"Voiceover Generation & Time-Fitting ({cfg.tts_engine}: {cfg.voice})")
    if on_step:
        on_step(3, "TTS & ghép audio")
    tts = get_tts_engine(cfg)
    chunk_audio_paths: list[Path] = []
    for chunk, segs in zip(chunks, segments_by_chunk):
        if checkpoint.chunk_done(chunk.index):
            log.info("Chunk #%d/%d: audio đã ghép xong (RESUME) → bỏ qua.", chunk.index + 1, len(chunks))
        else:
            log.info("Chunk #%d/%d: tổng hợp giọng đọc cho %d đoạn...", chunk.index + 1, len(chunks), len(segs))
        chunk_audio_paths.append(_ensure_tts_and_assemble(cfg, chunk, checkpoint, segs, tts))
        if on_chunk_progress:
            on_chunk_progress(chunk.index + 1, len(chunks))
        gc.collect()
    if cfg.stop_after == 3:
        log.info("Dừng sau bước TTS theo yêu cầu (--stop-after 3). Audio từng chunk: %s", cfg.chunk_audio_dir)
        return outputs

    # ── Ghép cuối (streaming) + mux ────────────────────────────────────────────
    banner(4, PIPELINE_STEPS, f"Ghép {len(chunks)} chunk (streaming, concat demuxer) + Ducking + Muxing")
    if on_step:
        on_step(4, "Ghép & Mux")
    windows = [(c.start_sec, c.end_sec) for c in chunks]
    video_muxer.mux_video_streaming(cfg, media, windows, chunk_audio_paths)
    outputs["voiceover"] = cfg.voiceover_path
    outputs["video"] = cfg.final_video_path
    _aggregate_sync_report(cfg, chunks)

    if not cfg.keep_temp:
        for f in cfg.frames_dir.rglob("frame_*.jpg"):
            f.unlink(missing_ok=True)
    log.info("✔ Xong trong %.1fs (%d chunk). Video: %s", time.perf_counter() - t0, len(chunks), cfg.final_video_path)
    return outputs
