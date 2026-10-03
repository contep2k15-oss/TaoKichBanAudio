"""python tests/test_youtube_pipeline_e2e.py — luồng YouTube-link chạy TRỌN qua long_video_pipeline.run().
Giả lập download_youtube_video() (copy file cục bộ) vì sandbox không có mạng ra youtube.com — phần định danh
URL, song song hoá, LƯU KỊCH BẢN, phân phối vào chunk và RESUME đều là mã thật.

Các kịch bản (3) và (4) TÁI HIỆN LỖI THẬT đã gặp: kịch bản Gemini chỉ nằm trong bộ nhớ nên app bị đóng giữa
chừng là mất hết, lần chạy sau âm thầm rơi về đường trích-frame-từng-chunk chậm (rất chậm với video 4K)."""
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_tts  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
import long_video_pipeline  # noqa: E402
import main as app  # noqa: E402
import youtube_source  # noqa: E402
from config import Config  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from smoke_test import FakeCommunicate, make_video  # noqa: E402
from utils import format_timestamp, parse_timestamp, probe_media, setup_logging  # noqa: E402

gwd.GeminiWebDriver._backoff = lambda self, attempt: None  # type: ignore[method-assign]


def check(name, cond):
    assert cond, name
    print("  ✓", name)


DURATION = 20.0
URL = "https://www.youtube.com/watch?v=fakeID12345"
counts = {"youtube": 0, "frame": 0, "download": 0}
download_heights: list[int] = []


def reset():
    counts.update(youtube=0, frame=0, download=0)
    download_heights.clear()


def fake_reply(req: dict) -> str:
    prompt = req["prompt"]
    if "XEM TRỰC TIẾP" in prompt and "youtube.com/watch" in prompt:
        counts["youtube"] += 1
        segs, t, i = [], 0.5, 1
        while t < DURATION - 1.0:
            segs.append({"id": i, "start_time": format_timestamp(t), "end_time": format_timestamp(t + 1.2),
                        "text": "một hai ba bốn", "tone": "vui ve"})
            t, i = t + 2.5, i + 1
        return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"
    # prompt dựa trên ẢNH (đường cũ, chậm) — KHÔNG được xuất hiện trong luồng YouTube
    counts["frame"] += 1
    stamps = [parse_timestamp(m) for m in re.findall(r"Image \d+: (\d\d:\d\d:\d\d\.\d+)", prompt)]
    segs = [{"id": k, "start_time": format_timestamp(t - 0.4), "end_time": format_timestamp(t + 0.4),
            "text": "một hai ba bốn", "tone": "vui ve"} for k, t in enumerate(stamps, start=1)]
    return "```json\n" + json.dumps(segs, ensure_ascii=False) + "\n```"


def fake_download(url: str, out_path: Path, **kw) -> Path:
    counts["download"] += 1
    download_heights.append(kw.get("max_height"))
    time.sleep(0.5)
    shutil.copy(SRC_VIDEO, out_path)
    return out_path


srv = FakeGeminiServer()
srv.reply_fn = fake_reply
edge_tts.Communicate = FakeCommunicate
youtube_source.download_youtube_video = fake_download

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    SRC_VIDEO = tmp / "src_for_fake_download.mp4"
    make_video(SRC_VIDEO, DURATION, with_audio=False)
    (tmp / "profile").mkdir()
    (tmp / "profile" / ".keep").write_text("x")

    import config as config_mod
    config_mod.DEFAULT_PROFILE_DIR = tmp / "profile"
    orig_post = Config.__post_init__

    def patched_post(self):
        orig_post(self)
        self.gemini_url = srv.url()
    Config.__post_init__ = patched_post

    def make_cfg(out_name: str, **kw) -> Config:
        cfg = Config(video_path=None, youtube_url=URL, output_dir=tmp / out_name, engine="web",
                     browser_channel="chromium", headless=True, web_stable_sec=0.5, web_response_timeout_sec=25,
                     web_upload_timeout_sec=15, web_delay_between_prompts_sec=0, web_retries=2,
                     chunk_target_sec=8.0, chunk_tolerance_sec=3.0, min_chunk_sec=4.0, **kw)
        setup_logging(cfg.log_path)
        return cfg

    # ══ (1) chạy mới: 1 lần hỏi Gemini, 1 lần tải, KHÔNG có prompt dựa trên ảnh, kịch bản được LƯU ══
    print("— (1) chạy mới")
    reset()
    cfg = make_cfg("out_yt")
    out = app.run_pipeline(cfg)
    check("có video kết quả", out["video"] is not None and Path(out["video"]).is_file())
    check("hỏi Gemini về link ĐÚNG 1 lần", counts["youtube"] == 1)
    check("KHÔNG có prompt dựa trên ảnh nào (không rơi về đường trích frame chậm)", counts["frame"] == 0)
    check("tải video đúng 1 lần, với độ cao tối đa mặc định 1080", counts["download"] == 1 and download_heights == [1080])
    check("KỊCH BẢN ĐÃ ĐƯỢC LƯU RA ĐĨA (youtube_script.json)", (cfg.workdir / "youtube_script.json").is_file())
    check("video tải về đặt tên theo độ cao (youtube_source_1080p.mp4)", (cfg.workdir / "youtube_source_1080p.mp4").is_file())
    info = probe_media(out["video"])
    check(f"video cuối đúng độ dài gốc: {info.duration_sec:.1f}s ≈ {DURATION}s", abs(info.duration_sec - DURATION) < 0.5)

    # ══ (2) chạy lại: KHÔNG hỏi lại, KHÔNG tải lại ══
    print("— (2) resume (cùng cấu hình)")
    reset()
    out2 = app.run_pipeline(cfg)
    check("resume: không hỏi lại Gemini, không tải lại, không prompt ảnh",
          counts == {"youtube": 0, "frame": 0, "download": 0})
    check("resume: vẫn ra video", out2["video"] is not None and Path(out2["video"]).is_file())

    # ══ (3) TÁI HIỆN LỖI THẬT: app sập SAU KHI nhận kịch bản, TRƯỚC KHI phân phối/checkpoint ══
    print("— (3) sập giữa chừng sau khi nhận kịch bản")
    reset()
    original_distribute = long_video_pipeline._distribute_segments_to_chunks

    def crash(*a, **k):
        raise RuntimeError("giả lập: app bị đóng/sập giữa chừng")
    long_video_pipeline._distribute_segments_to_chunks = crash
    cfg3 = make_cfg("out_yt3")
    try:
        app.run_pipeline(cfg3)
        raise AssertionError("phải sập")
    except RuntimeError:
        pass
    finally:
        long_video_pipeline._distribute_segments_to_chunks = original_distribute
    check("dù sập, kịch bản ĐÃ được lưu ra đĩa trước đó", (cfg3.workdir / "youtube_script.json").is_file())
    check("lần chạy đầu: đã hỏi Gemini 1 lần + tải 1 lần", counts["youtube"] == 1 and counts["download"] == 1)

    cfg3b = make_cfg("out_yt3")   # cfg MỚI hoàn toàn — như khi mở lại app (không giữ trạng thái nào trong bộ nhớ)
    out3 = app.run_pipeline(cfg3b)
    check("sau khi mở lại: KHÔNG hỏi lại Gemini (dùng kịch bản đã lưu)", counts["youtube"] == 1)
    check("sau khi mở lại: KHÔNG tải lại video", counts["download"] == 1)
    check("sau khi mở lại: TUYỆT ĐỐI không rơi về đường trích frame + prompt ảnh chậm", counts["frame"] == 0)
    check("sau khi mở lại: vẫn ra video hoàn chỉnh", out3["video"] is not None and Path(out3["video"]).is_file())

    # ══ (4) TRẠNG THÁI THẬT của người dùng: đã có video tải sẵn nhưng KHÔNG có kịch bản đã lưu, không checkpoint ══
    print("— (4) có video cache nhưng chưa có kịch bản đã lưu")
    reset()
    cfg4 = make_cfg("out_yt4")
    cfg4.workdir.mkdir(parents=True, exist_ok=True)
    shutil.copy(SRC_VIDEO, youtube_source.local_video_path(cfg4.workdir, 1080))
    out4 = app.run_pipeline(cfg4)
    check("chỉ hỏi Gemini phần kịch bản đúng 1 lần", counts["youtube"] == 1)
    check("KHÔNG tải lại video đã có sẵn", counts["download"] == 0)
    check("KHÔNG rơi về đường trích frame + prompt ảnh chậm (đúng lỗi từng gặp)", counts["frame"] == 0)
    check("ra video hoàn chỉnh", out4["video"] is not None and Path(out4["video"]).is_file())
    check("log nói rõ lý do chỉ hỏi phần kịch bản", "CHƯA có kịch bản đã lưu" in cfg4.log_path.read_text(encoding="utf-8"))
    check("kịch bản được lưu để lần sau khỏi hỏi lại", (cfg4.workdir / "youtube_script.json").is_file())

    # ══ (5) đổi chất lượng tải: tải lại file mới đúng độ cao, kịch bản (cùng link) dùng lại ══
    print("— (5) đổi độ cao tối đa sang 720")
    reset()
    cfg5 = make_cfg("out_yt", youtube_max_height=720)   # cùng workdir với (1): đã có kịch bản đã lưu cho link này
    out5 = app.run_pipeline(cfg5)
    check("tải file mới với độ cao tối đa 720", counts["download"] == 1 and download_heights == [720])
    check("KHÔNG hỏi lại Gemini (kịch bản cùng link đã lưu)", counts["youtube"] == 0)
    check("file tên theo độ cao mới", (cfg5.workdir / "youtube_source_720p.mp4").is_file())
    check("ra video hoàn chỉnh", out5["video"] is not None and Path(out5["video"]).is_file())

    # ══ (6) đổi sang link KHÁC: kịch bản cũ KHÔNG được dùng nhầm ══
    print("— (6) link khác không dùng nhầm kịch bản cũ")
    cfg6 = make_cfg("out_yt")
    cfg6.youtube_url = "https://www.youtube.com/watch?v=OTHER_LINK_1"
    check("kịch bản đã lưu của link cũ KHÔNG được nạp cho link mới",
          long_video_pipeline._load_youtube_script(cfg6) is None)

srv.close()
print("TẤT CẢ PASS ✔")
