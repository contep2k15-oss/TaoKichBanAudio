"""BƯỚC 4 — audio_assembler: Audio Timeline Assembly (kiến trúc Pydub — VideoLingo).

KHÔNG dùng `ffmpeg -filter_complex` cho N file lẻ (tránh WinError 206). Thay vào đó:
  1. Tạo Audio Track trống bằng đúng độ dài video:  AudioSegment.silent(duration=total_video_ms)
  2. Với mỗi đoạn TTS: tính vị trí start_time (ms) rồi `overlay` thẳng vào track trong bộ nhớ Python
  3. Xuất MỘT file duy nhất: final_voiceover.mp3
Đồng thời kiểm tra lệch timestamp và ghi sync_report.json.

NGẪU NHIÊN HOÁ KHOẢNG NGHỈ GIỮA CÂU (`cfg.min_pause_sec`/`cfg.max_pause_sec`):
Gemini tự chọn mốc `start_time` cho từng câu theo nội dung cảnh quay — khoảng cách "danh nghĩa" giữa hai
câu (từ lúc audio câu trước dứt tới `start_time` câu sau) vì vậy vốn đã không cố định, nhưng Gemini có xu
hướng bám khá sát ngưỡng tối thiểu đã ra lệnh trong prompt (~0.2s) ở nhiều câu, nghe lặp nhịp, máy móc.
Ở ĐÂY, ngay trước khi đặt một đoạn lên track, nếu khoảng trống THỰC TẾ (giữa điểm audio câu trước vừa đặt
xong kết thúc, và `start_time` gốc của câu này) LỚN HƠN một khoảng nghỉ ngẫu nhiên trong
[min_pause_sec, max_pause_sec], ta CHỦ ĐỘNG RÚT NGẮN khoảng trống đó xuống đúng bằng khoảng nghỉ ngẫu
nhiên vừa chọn — tức kéo câu này sớm lên. Tuyệt đối KHÔNG BAO GIỜ đẩy trễ một câu hay "bịa" thêm khoảng
lặng vượt quá phần dư sẵn có — chỉ rút ngắn phần dư đã tồn tại tự nhiên, nên không ảnh hưởng gì tới đồng
bộ hình ảnh (câu chỉ có thể sớm lên tối đa bằng khoảng trống vốn đã có). Nếu khoảng trống tự nhiên đã nhỏ
hơn hoặc bằng khoảng nghỉ ngẫu nhiên chọn được, giữ nguyên, không có gì để rút ngắn.
"""
from __future__ import annotations

import logging
import random
from dataclasses import asdict, dataclass
from pathlib import Path

from pydub import AudioSegment

from config import Config
from tts_engine import TTSResult
from utils import PipelineError, run_cmd, write_json

log = logging.getLogger("svvc.assemble")

TRACK_SAMPLE_RATE = 24000     # khớp đầu ra của Edge-TTS
DRIFT_TOLERANCE_MS = 10       # lệch điểm bắt đầu KHÔNG GIẢI THÍCH ĐƯỢC cho phép (đã trừ phần rút ngắn nghỉ chủ động)
FADE_OUT_ON_CLIP_MS = 60


@dataclass
class PlacedSegment:
    id: int
    planned_start_ms: int
    planned_end_ms: int
    placed_start_ms: int
    placed_end_ms: int
    audio_len_ms: int
    start_drift_ms: int
    overrun_ms: int            # phần audio vượt quá end_time kế hoạch (>0: lấn sang khoảng trống)
    clipped_ms: int            # phần bị cắt bỏ vì chạm đoạn kế tiếp / hết video
    pause_shrink_ms: int       # phần khoảng nghỉ tự nhiên đã CHỦ ĐỘNG rút ngắn (ngẫu nhiên hoá nhịp nghỉ)
    speed: float
    status: str


def assemble_voiceover_track(results: list[TTSResult], total_video_ms: int, cfg: Config, *,
                             out_path: Path | None = None, report_path: Path | None = None,
                             rng: random.Random | None = None
                             ) -> tuple[AudioSegment, list[PlacedSegment]]:
    """`out_path`/`report_path`: mặc định `cfg.voiceover_path`/`cfg.sync_report_path` (video ngắn, 1 chunk
    duy nhất). Khi xử lý video dài theo từng MACRO-CHUNK, long_video_pipeline.py truyền đường dẫn RIÊNG cho
    từng chunk (`work/chunk_audio/chunk_0007.mp3`) để không ghi đè lẫn nhau — `total_video_ms` khi đó là độ
    dài của CHUNK (không phải cả video), và `results[i].start_sec` đã được dịch về mốc 0 = đầu chunk.
    `rng`: bộ sinh số ngẫu nhiên cho khoảng nghỉ — truyền vào (vd `random.Random(seed)`) để kiểm thử có thể
    lặp lại; mặc định dùng `random.Random()` mới (không seed, mỗi lần chạy một nhịp nghỉ khác nhau)."""
    out_path = out_path or cfg.voiceover_path
    report_path = report_path or cfg.sync_report_path
    rng = rng or random.Random()
    if total_video_ms <= 0:
        raise PipelineError("Độ dài video không hợp lệ.")
    track = AudioSegment.silent(duration=total_video_ms, frame_rate=TRACK_SAMPLE_RATE)
    usable = sorted((r for r in results if r.fitted_path and r.status not in ("failed", "skipped")),
                    key=lambda r: r.start_sec)
    usable_ids = {r.id for r in usable}
    skipped = [r.id for r in results if r.id not in usable_ids]
    if skipped:
        log.warning("Bỏ qua %d đoạn không có audio: %s", len(skipped), skipped)
    if not usable:
        raise PipelineError("Không có đoạn audio nào để ghép.")

    placed: list[PlacedSegment] = []
    prev_end_ms = 0
    for i, r in enumerate(usable):
        original_pos = int(round(r.start_sec * 1000))
        planned_end = original_pos + int(round(r.planned_sec * 1000))
        if original_pos >= total_video_ms:
            log.warning("Đoạn %d bắt đầu (%dms) sau khi video kết thúc → bỏ.", r.id, original_pos)
            continue

        # ── ngẫu nhiên hoá khoảng nghỉ: chỉ RÚT NGẮN khoảng trống tự nhiên đã có, không bao giờ tạo thêm ──
        pause_shrink = 0
        pos = original_pos
        if i > 0:
            natural_gap = original_pos - prev_end_ms
            if natural_gap > 0:
                target_pause = int(round(rng.uniform(cfg.min_pause_sec, cfg.max_pause_sec) * 1000))
                applied_pause = min(target_pause, natural_gap)
                pos = prev_end_ms + applied_pause
                pause_shrink = natural_gap - applied_pause
                if pause_shrink > 0:
                    log.debug("Đoạn %d: rút ngắn khoảng nghỉ %dms → %dms (nghỉ ngẫu nhiên mục tiêu %dms).",
                             r.id, natural_gap, applied_pause, target_pause)

        try:
            seg_audio = (AudioSegment.from_file(r.fitted_path)
                         .set_frame_rate(TRACK_SAMPLE_RATE).set_channels(1).set_sample_width(2))
        except Exception as e:  # noqa: BLE001
            log.error("Đoạn %d: không đọc được %s (%s) → bỏ.", r.id, r.fitted_path, e)
            continue

        # Giới hạn: không lấn qua điểm bắt đầu GỐC (chưa rút ngắn) của đoạn kế tiếp / hết video — an toàn vì
        # sau khi rút ngắn, đoạn kế tiếp chỉ có thể xích lại GẦN HƠN, không bao giờ lùi xa hơn mốc gốc này.
        limit = int(round(usable[i + 1].start_sec * 1000)) if i + 1 < len(usable) else total_video_ms
        limit = min(limit, total_video_ms)
        room = max(0, limit - pos)
        original_len, clipped = len(seg_audio), 0
        if original_len > room:
            clipped = original_len - room
            seg_audio = seg_audio[:room].fade_out(min(FADE_OUT_ON_CLIP_MS, max(1, room // 4)))
            log.warning("Đoạn %d dài %dms nhưng chỉ còn %dms trước đoạn kế → cắt %dms cuối (fade-out).",
                        r.id, original_len, room, clipped)

        track = track.overlay(seg_audio, position=pos)      # ← ghi thẳng vào đúng mili-giây

        end = pos + len(seg_audio)
        p = PlacedSegment(id=r.id, planned_start_ms=original_pos, planned_end_ms=planned_end, placed_start_ms=pos,
                          placed_end_ms=end, audio_len_ms=original_len, start_drift_ms=pos - original_pos,
                          overrun_ms=max(0, pos + original_len - planned_end), clipped_ms=clipped,
                          pause_shrink_ms=pause_shrink, speed=r.speed, status=r.status)
        placed.append(p)
        prev_end_ms = end
        log.debug("Đoạn %d: %dms → %dms (kế hoạch kết thúc %dms, speed %.2f×, rút ngắn nghỉ %dms)",
                 r.id, pos, end, planned_end, r.speed, pause_shrink)

    if len(track) != total_video_ms:  # overlay không được đổi độ dài; nếu lệch là lỗi nghiêm trọng
        raise PipelineError(f"Audio track lệch độ dài: {len(track)}ms ≠ video {total_video_ms}ms")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        track.export(out_path, format="mp3", bitrate=cfg.audio_bitrate)
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"Xuất {out_path.name} thất bại: {e}") from e
    _verify_and_report(placed, out_path, total_video_ms, cfg, report_path)
    return track, placed


def _verify_and_report(placed: list[PlacedSegment], mp3_path: Path, total_video_ms: int, cfg: Config,
                       report_path: Path) -> None:
    # Lệch điểm bắt đầu THỰC SỰ không giải thích được: trừ đi phần đã CHỦ ĐỘNG rút ngắn (pause_shrink_ms)
    # — nếu không, mọi đoạn có ngẫu nhiên hoá nghỉ sẽ luôn bị báo "lệch nhịp" dù đó là hành vi có chủ đích.
    bad_drift = [p for p in placed if abs(p.start_drift_ms + p.pause_shrink_ms) > DRIFT_TOLERANCE_MS]
    overruns = [p for p in placed if p.overrun_ms > 0]
    clipped = [p for p in placed if p.clipped_ms > 0]
    overlaps = [(a.id, b.id) for a, b in zip(placed, placed[1:]) if a.placed_end_ms > b.placed_start_ms]
    n_shrunk = sum(1 for p in placed if p.pause_shrink_ms > 0)

    log.info("Kiểm tra đồng bộ: %d đoạn | lệch điểm bắt đầu >%dms (đã trừ rút ngắn nghỉ chủ động): %d | "
             "lấn qua end_time: %d | bị cắt: %d | chồng lấn: %d | đã rút ngắn nghỉ ở %d đoạn",
             len(placed), DRIFT_TOLERANCE_MS, len(bad_drift), len(overruns), len(clipped), len(overlaps), n_shrunk)
    for p in bad_drift:
        log.warning("  #%d lệch %+dms so với start_time (không giải thích được bởi rút ngắn nghỉ).",
                    p.id, p.start_drift_ms + p.pause_shrink_ms)
    for p in overruns:
        log.info("  #%d lấn %dms qua end_time (vẫn nằm trong khoảng trống trước đoạn kế).", p.id, p.overrun_ms)

    mp3_ms = None
    try:
        mp3_ms = probe_media_audio_ms(mp3_path)
        delta = mp3_ms - total_video_ms
        (log.warning if abs(delta) > 150 else log.info)(
            "Độ dài final_voiceover.mp3 = %dms, video = %dms (chênh %+dms).", mp3_ms, total_video_ms, delta)
    except Exception as e:  # noqa: BLE001
        log.debug("Không đo được độ dài mp3: %s", e)

    write_json(report_path, {
        "total_video_ms": total_video_ms, "voiceover_mp3_ms": mp3_ms,
        "summary": {"segments": len(placed), "start_drift_over_tolerance": len(bad_drift),
                    "overruns": len(overruns), "clipped": len(clipped), "overlaps": len(overlaps),
                    "pause_shrunk_segments": n_shrunk},
        "segments": [asdict(p) for p in placed]})
    log.info("Đã xuất %s và báo cáo %s", mp3_path.name, report_path.name)


def probe_media_audio_ms(path: Path) -> int:
    """Độ dài file audio (ms) qua ffprobe."""
    proc = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                    "default=noprint_wrappers=1:nokey=1", path], "ffprobe audio")
    return int(round(float(proc.stdout.strip()) * 1000))
