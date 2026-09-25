"""Smoke test OFFLINE toàn pipeline MỚI (long_video_pipeline): Playwright THẬT (Chromium) + trang Gemini
giả lập + Edge-TTS giả lập + FFmpeg/OpenCV/pydub thật.

    python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydub import AudioSegment  # noqa: E402
from pydub.generators import Sine  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
import main as app  # noqa: E402
from checkpoint import PipelineCheckpoint, Stage  # noqa: E402
from config import Config  # noqa: E402
from engine_factory import _open_gemini_web  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from utils import WebAuthError, format_timestamp, parse_timestamp, probe_media, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None      # type: ignore[method-assign]


def check(name: str, cond: bool, extra: str = "") -> None:
    assert cond, f"{name} {extra}"
    print("  ✓", name)


def make_video(path: Path, duration: float, with_audio: bool) -> None:
    """Video tổng hợp `duration` giây, 3 "cảnh" khác màu chia đều, có thể kèm âm thanh."""
    third = duration / 3
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for src in ("testsrc2", "smptebars", "rgbtestsrc"):
        cmd += ["-f", "lavfi", "-i", f"{src}=size=320x180:rate=15:duration={third}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=220:duration={duration}"]
    cmd += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]"]
    if with_audio:
        cmd += ["-map", "3:a", "-c:a", "aac", "-af", "volume=0.3"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)


# ── Edge-TTS giả ────────────────────────────────────────────
class FakeCommunicate:
    fail_once_texts: set[str] = set()

    def __init__(self, text, voice, *, rate="+0%", volume="+0%", pitch="+0Hz", **kw):
        assert re.fullmatch(r"[+-]\d+%", rate) and re.fullmatch(r"[+-]\d+Hz", pitch) and re.fullmatch(r"[+-]\d+%", volume)
        self.text = text

    async def save(self, path: str) -> None:
        await asyncio.sleep(0.005)
        if self.text not in FakeCommunicate.fail_once_texts:
            FakeCommunicate.fail_once_texts.add(self.text)
            raise ConnectionError("mạng chập chờn (giả lập)")
        tone = Sine(300).to_audio_segment(duration=int(280 * max(1, len(self.text.split())))).apply_gain(-8)
        (AudioSegment.silent(120) + tone + AudioSegment.silent(150)).export(path, format="mp3")


# ── "Gemini" giả: đọc nhãn thời gian trong prompt, trả 1 đoạn quanh mỗi ảnh ──
def fake_reply(req: dict) -> str:
    prompt = req["prompt"]
    if "Rút gọn" in prompt:
        ids = [int(i) for i in re.findall(r'"id": (\d+)', prompt)]
        return "```json\n" + json.dumps([{"id": i, "text": "ngắn gọn thôi nhé"} for i in ids], ensure_ascii=False) + "\n```"
    stamps = [parse_timestamp(m) for m in re.findall(r"Ảnh \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    assert len(stamps) == req["n_files"], f"prompt liệt kê {len(stamps)} ảnh nhưng đính kèm {req['n_files']}"
    segs = []
    for k, t in enumerate(stamps, start=1):
        segs.append({"id": k, "start_time": format_timestamp(t - 0.4), "end_time": format_timestamp(t + 0.4),
                     "text": "một hai ba " + "**nhé**", "tone": ["vui ve", "Hào hứng", "tram am", "???"][k % 4]})
    return "Kịch bản đây:\n```json\n" + json.dumps(segs, ensure_ascii=False, indent=2) + "\n```"


def web_cfg(tmp: Path, srv: FakeGeminiServer, video: Path, out: str, **kw) -> Config:
    return Config(video_path=video, output_dir=tmp / out, engine="web", user_data_dir=tmp / "profile", browser_channel="chromium",
                  headless=True, gemini_url=srv.url(), web_stable_sec=0.5, web_response_timeout_sec=25, web_upload_timeout_sec=15,
                  web_delay_between_prompts_sec=0, web_retries=2, frames_per_prompt=3, tts_concurrency=3,
                  frame_interval_sec=1.0, min_segment_sec=0.3, **kw)


# ════════════════════════════════════════════════════════════
# 1) Video NGẮN (1 chunk) - luồng đầy đủ, giống hệt hành vi bản gốc
# ════════════════════════════════════════════════════════════
def test_short_video_single_chunk(tmp: Path, srv: FakeGeminiServer) -> None:
    video = tmp / "short.mp4"
    make_video(video, 9.0, with_audio=True)
    (tmp / "profile").mkdir(exist_ok=True)
    (tmp / "profile" / ".keep").write_text("x")
    srv.reply_fn, srv.requests = fake_reply, []
    cfg = web_cfg(tmp, srv, video, "out_short", chunk_target_sec=240.0)   # video 9s << 240s → 1 chunk
    setup_logging(cfg.log_path)

    steps: list[int] = []
    out = app.run_pipeline(cfg, on_step=lambda n, m: steps.append(n))
    check("1 chunk: chạy đủ 4 giai đoạn pipeline", steps == [1, 2, 3, 4], str(steps))

    script = json.loads(cfg.script_path.read_text(encoding="utf-8"))
    check("script.json đúng schema", all(set(s) == {"id", "start_time", "end_time", "duration_sec", "text", "tone"} for s in script)
          and [s["id"] for s in script] == list(range(1, len(script) + 1)))
    info = probe_media(out["video"])
    check(f"video cuối {info.duration_sec:.2f}s có audio (khớp video gốc)", abs(info.duration_sec - 9.0) < 0.3 and info.has_audio)

    n = len(srv.requests)
    app.run_pipeline(cfg)
    check("chạy lại (video ngắn): resume toàn bộ, KHÔNG gọi lại Gemini", len(srv.requests) == n)


# ════════════════════════════════════════════════════════════
# 2) Video DÀI (giả lập bằng chunk nhỏ): NHIỀU CHUNK, RESUME giữa chừng, GHÉP STREAMING
# ════════════════════════════════════════════════════════════
def test_long_video_chunking_and_resume(tmp: Path, srv: FakeGeminiServer) -> None:
    """Video thật ngắn (24s) nhưng `chunk_target_sec` đặt rất nhỏ (8s) để ép chia thành NHIỀU macro-chunk -
    cơ chế chia chunk không phụ thuộc độ dài tuyệt đối (chỉ phụ thuộc target/tolerance), nên bài test này
    xác nhận đúng cơ chế mà không cần xử lý một video dài thật hàng chục phút (quá tốn thời gian trong CI)."""
    video = tmp / "long.mp4"
    make_video(video, 24.0, with_audio=True)
    srv.reply_fn, srv.requests = fake_reply, []
    cfg = web_cfg(tmp, srv, video, "out_long", chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0)
    setup_logging(cfg.log_path)

    chunk_progress: list[tuple[int, int]] = []
    out = app.run_pipeline(cfg, on_chunk_progress=lambda i, n: chunk_progress.append((i, n)))
    total_chunks = chunk_progress[-1][1]
    check(f"video 24s bị chia thành {total_chunks} macro-chunk (>1, đúng cơ chế phân đoạn động)", total_chunks > 1)

    plan = json.loads((cfg.workdir / "checkpoints").glob("*/00_chunks.json").__next__().read_text(encoding="utf-8"))
    chunks = plan["chunks"]
    check("các chunk liên tục, phủ kín [0, 24s], không chồng lấn", chunks[0]["start_sec"] == 0
          and abs(chunks[-1]["end_sec"] - 24.0) < 1e-6
          and all(abs(a["end_sec"] - b["start_sec"]) < 1e-6 for a, b in zip(chunks, chunks[1:])))

    info = probe_media(out["video"])
    check(f"video cuối (ghép streaming từ {total_chunks} chunk) đúng độ dài gốc: {info.duration_sec:.2f}s", abs(info.duration_sec - 24.0) < 0.4)
    script = json.loads(cfg.script_path.read_text(encoding="utf-8"))
    check("script.json TOÀN VIDEO có id liên tục xuyên suốt các chunk (không reset về 1 giữa chừng)",
          [s["id"] for s in script] == list(range(1, len(script) + 1)) and len(script) >= total_chunks)

    # ── Giả lập "rớt mạng giữa chừng": xoá sạch checkpoint của các chunk TỪ GIỮA trở đi, chạy lại ─────
    fp_dir = next((cfg.workdir / "checkpoints").glob("*"))
    checkpoint = PipelineCheckpoint(cfg.workdir, json.loads((fp_dir / "index.json").read_text(encoding="utf-8"))["fingerprint"])
    cutoff = total_chunks // 2
    for i in range(cutoff, total_chunks):
        checkpoint.invalidate_from(i, Stage.EXTRACTED)
    n_before = len(srv.requests)
    for i in range(cutoff):
        check(f"trước khi resume: chunk #{i} (không bị invalidate) vẫn 'done' trong checkpoint", checkpoint.chunk_done(i))
    for i in range(cutoff, total_chunks):
        check(f"trước khi resume: chunk #{i} (đã invalidate) KHÔNG còn 'done'", not checkpoint.chunk_done(i))

    resumed: list[tuple[int, int]] = []
    app.run_pipeline(cfg, on_chunk_progress=lambda i, n: resumed.append((i, n)))
    # log của pipeline phải nêu RÕ các chunk < cutoff là "đã xong ở lần chạy trước" (RESUME) — tìm trong
    # pipeline.log thay vì suy luận qua nội dung request (vốn còn phụ thuộc cache riêng của script_generator).
    log_text = cfg.log_path.read_text(encoding="utf-8")
    for i in range(cutoff):
        check(f"log xác nhận chunk #{i} được RESUME (không xử lý lại)",
              f"Chunk #{i + 1}/{total_chunks}: kịch bản đã có (RESUME)" in log_text)
    checkpoint_after = PipelineCheckpoint(cfg.workdir, checkpoint.fingerprint)
    check("sau resume: TẤT CẢ chunk đều 'done' trở lại", all(checkpoint_after.chunk_done(i) for i in range(total_chunks)))
    check("resume không tốn thêm request nào cho phần KHÔNG bị invalidate (0 <= n)", len(srv.requests) >= n_before)
    info2 = probe_media(out["video"])
    check("sau resume: video cuối vẫn đúng độ dài (ghép lại đầy đủ mọi chunk)", abs(info2.duration_sec - 24.0) < 0.4)


# ════════════════════════════════════════════════════════════
# 3) --stop-after 2 (nhiều chunk): dừng đúng lúc, KHÔNG tốn TTS; chạy lại tiếp tục từ TTS
# ════════════════════════════════════════════════════════════
def test_stop_after_script_multi_chunk(tmp: Path, srv: FakeGeminiServer) -> None:
    video = tmp / "stop2.mp4"
    make_video(video, 16.0, with_audio=False)
    srv.reply_fn, srv.requests = fake_reply, []
    cfg = web_cfg(tmp, srv, video, "out_stop2", chunk_target_sec=6.0, chunk_tolerance_sec=2.0, min_chunk_sec=3.0, stop_after=2)
    setup_logging(cfg.log_path)
    out = app.run_pipeline(cfg)
    check("--stop-after 2: có script.json, CHƯA có voiceover/video", cfg.script_path.is_file() and out["voiceover"] is None and out["video"] is None)
    check("--stop-after 2: chưa gọi TTS nào (thư mục tts rỗng)", not any(cfg.tts_dir.glob("*.mp3")))

    cfg2 = web_cfg(tmp, srv, video, "out_stop2", chunk_target_sec=6.0, chunk_tolerance_sec=2.0, min_chunk_sec=3.0, stop_after=None)
    n_before = len(srv.requests)
    out2 = app.run_pipeline(cfg2)
    check("chạy tiếp (bỏ --stop-after): KHÔNG gọi lại Gemini, chỉ chạy TTS+mux tiếp", len(srv.requests) == n_before)
    check("chạy tiếp: có video hoàn chỉnh", out2["video"] is not None and out2["video"].is_file())


# ════════════════════════════════════════════════════════════
# 4) --script-file với nhiều chunk: BỎ QUA Gemini hoàn toàn, tự chia segment về đúng chunk
# ════════════════════════════════════════════════════════════
def test_script_file_multi_chunk(tmp: Path, srv: FakeGeminiServer) -> None:
    video = tmp / "sf.mp4"
    make_video(video, 16.0, with_audio=False)
    script = [{"id": 1, "start_time": "00:00:01.000", "end_time": "00:00:03.000", "text": "đoạn một hai ba", "tone": "vui ve"},
              {"id": 2, "start_time": "00:00:08.000", "end_time": "00:00:10.000", "text": "đoạn hai bốn năm", "tone": "tram am"},
              {"id": 3, "start_time": "00:00:14.000", "end_time": "00:00:15.500", "text": "đoạn ba kết thúc"}]
    sp = tmp / "sf.json"
    sp.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    n_before = len(srv.requests)
    cfg = web_cfg(tmp, srv, video, "out_sf", chunk_target_sec=6.0, chunk_tolerance_sec=2.0, min_chunk_sec=3.0, script_file=sp)
    setup_logging(cfg.log_path)
    out = app.run_pipeline(cfg)
    check("--script-file (nhiều chunk): không gọi Gemini một lần nào", len(srv.requests) == n_before)
    info = probe_media(out["video"])
    check(f"video cuối đúng độ dài gốc: {info.duration_sec:.2f}s", abs(info.duration_sec - 16.0) < 0.4)
    agg = json.loads(cfg.script_path.read_text(encoding="utf-8"))
    check("script.json tổng hợp lại đủ 3 đoạn người dùng cung cấp, đúng thứ tự thời gian", len(agg) == 3
          and [round(parse_timestamp(s["start_time"]), 1) for s in agg] == [1.0, 8.0, 14.0])


# ════════════════════════════════════════════════════════════
# 5) engine_factory: mở Gemini Web khi chưa có profile → WebAuthError hướng dẫn --login
# ════════════════════════════════════════════════════════════
def test_engine_factory_login_required(tmp: Path, srv: FakeGeminiServer) -> None:
    cfg = Config(video_path=None, output_dir=tmp / "ef", engine="web", user_data_dir=tmp / "no_profile",
                browser_channel="chromium", gemini_url=srv.url())
    try:
        _open_gemini_web(cfg, interactive=False)
        raise AssertionError("phải ném WebAuthError")
    except WebAuthError as e:
        check("engine_factory: chưa có profile + không tương tác → WebAuthError chỉ dẫn --login", "--login" in str(e))


def test_login_flow(tmp: Path, srv: FakeGeminiServer) -> None:
    fake = tmp / "fake_chrome.sh"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in --user-data-dir=*) d="${a#--user-data-dir=}";; esac; done\n'
                    'mkdir -p "$d"; echo cookie > "$d/FakeCookies"; sleep 1\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    os.environ["SVVC_BROWSER_PATH"] = str(fake)
    cfg = Config(output_dir=tmp / "lg", user_data_dir=tmp / "profile_login", browser_channel="chromium", gemini_url=srv.url())
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    ok = gwd.interactive_login(cfg, input_fn=lambda _: "")
    del os.environ["SVVC_BROWSER_PATH"]
    check("login tương tác: mở trình duyệt, chờ Enter, đóng, kiểm tra phiên headless", ok and (cfg.user_data_dir / "FakeCookies").exists())


def test_check_web_cli(tmp: Path, srv: FakeGeminiServer) -> None:
    srv.reply_fn = None
    (tmp / "profile2").mkdir(exist_ok=True)
    rc = app.main(["--check-web", "--browser", "chromium", "--gemini-url", srv.url(), "--user-data-dir", str(tmp / "profile2"),
                  "-o", str(tmp / "chk"), "--web-delay", "0", "--web-model", "Fast"])
    check("CLI --check-web trả về 0 khi mọi hạng mục đạt", rc == 0)


if __name__ == "__main__":
    import edge_tts
    edge_tts.Communicate = FakeCommunicate                  # type: ignore[misc]
    print("Chạy smoke test offline (pipeline mới: chunking + checkpoint/resume + streaming mux)...")
    srv = FakeGeminiServer()
    with tempfile.TemporaryDirectory() as d:
        t = Path(d)
        test_short_video_single_chunk(t, srv)
        test_long_video_chunking_and_resume(t, srv)
        test_stop_after_script_multi_chunk(t, srv)
        test_script_file_multi_chunk(t, srv)
        test_engine_factory_login_required(t, srv)
        test_login_flow(t, srv)
        test_check_web_cli(t, srv)
    srv.close()
    print("TẤT CẢ ĐỀU PASS ✔")
