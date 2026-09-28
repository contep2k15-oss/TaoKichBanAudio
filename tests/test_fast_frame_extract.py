"""python tests/test_fast_frame_extract.py — quét nhanh highlight: đúng số khung, đúng thời điểm, nhanh, có dự phòng."""
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cv2  # noqa: E402
import numpy as np  # noqa: E402

import video_processor as vp  # noqa: E402
from config import Config  # noqa: E402
from utils import probe_media  # noqa: E402

logging.disable(logging.CRITICAL)


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def make(path: Path, codec_args: list[str], dur: float = 60.0) -> None:
    """Video có MỐC THỜI GIAN NHÌN THẤY ĐƯỢC: mỗi giây một màu khác hẳn → suy ra thời điểm từ màu khung."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"color=c=black:size=640x360:rate=25:duration={dur},"
                    f"drawbox=x=0:y=0:w=iw:h=ih:color=red@1:t=fill:enable='lt(mod(t,20),10)'",
                    *codec_args, "-pix_fmt", "yuv420p", "-g", "50", str(path)], check=True, stderr=subprocess.DEVNULL)


with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg = Config(video_path=tmp / "x.mp4", output_dir=tmp / "out", frame_interval_sec=2.5, frame_max_width=320)

    for label, args in (("H.264", ["-c:v", "libx264", "-preset", "ultrafast"]),
                        ("AV1", ["-c:v", "libsvtav1", "-preset", "12"])):
        v = tmp / f"{label}.mp4"
        make(v, args)
        media = probe_media(v)
        check(f"[{label}] ffprobe nhận đúng codec: {media.video_codec}", media.video_codec == ("h264" if label == "H.264" else "av1"))

        n, step = 10, media.duration_sec / 10                  # 10 cửa sổ 6s, khung ở giữa mỗi cửa sổ
        out = tmp / f"o_{label}"
        t0 = time.perf_counter()
        samples = vp.extract_uniform_frames_ffmpeg(v, start=step / 2, step=step, count=n, duration=media.duration_sec,
                                                   out_dir=out, cfg=cfg)
        dt = time.perf_counter() - t0
        check(f"[{label}] đúng {n} khung (không gấp đôi), mất {dt:.1f}s", samples is not None and len(samples) == n)
        check(f"[{label}] thời điểm khung = giữa cửa sổ ({samples[0].timestamp_sec:.1f}s, {samples[-1].timestamp_sec:.1f}s)",
              abs(samples[0].timestamp_sec - step / 2) < 1e-6 and abs(samples[-1].timestamp_sec - (n - 0.5) * step) < 1e-6)
        check(f"[{label}] cửa sổ phủ đúng [0, {media.duration_sec:.0f}]", samples[0].window_start == 0.0 and abs(samples[-1].window_end - media.duration_sec) < 1e-6)
        check(f"[{label}] ảnh đọc được, đã thu nhỏ về ≤320px rộng",
              all(cv2.imread(str(s.path)) is not None and cv2.imread(str(s.path)).shape[1] <= 320 for s in samples))
        # đúng NỘI DUNG: video đỏ ở [0,10) và [20,30) và [40,50); đen ở còn lại — khung ở t=3,9,15,21,... phải khớp
        def is_red(s):
            im = cv2.imread(str(s.path)); px = im[im.shape[0] // 2, im.shape[1] // 2]      # BGR, điểm giữa ảnh
            return px[2] > 150 and px[0] < 100
        expect = lambda t: (t % 20) < 10
        check(f"[{label}] NỘI DUNG khung khớp đúng thời điểm (màu đỏ/đen đúng chỗ, nhận biết được cả lệch thời gian)",
              all(is_red(s) == expect(s.timestamp_sec) for s in samples))
        check(f"[{label}] không còn file tạm ff_*.jpg", not list(out.glob("ff_*.jpg")))
        check(f"[{label}] đặt tên theo quy ước frame_XXXX_ms.jpg", samples[0].path.name.startswith("frame_0001_"))

    # ĐỘ CHÍNH XÁC THỜI GIAN (chống tái phát lỗi `fps` căn theo lưới tuyệt đối → lệch +3s): video có độ sáng
    # tăng tuyến tính theo thời gian; hiệu chuẩn thang đo bằng các khung lấy chính xác, rồi suy ngược thời điểm
    # thật của từng khung do hàm trích ra và so với thời điểm nó KHẲNG ĐỊNH.
    ramp = tmp / "ramp.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=gray:size=320x180:rate=25:duration=60,geq=lum='clip(T*4,0,255)':cb=128:cr=128",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "50", str(ramp)],
                   check=True, stderr=subprocess.DEVNULL)
    ts = np.arange(4, 60, 2.0); xs = []
    for t in ts:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(ramp), "-frames:v", "1",
                        "-q:v", "2", str(tmp / "cal.jpg")], check=True, stderr=subprocess.DEVNULL)
        xs.append(float(cv2.imread(str(tmp / "cal.jpg"), cv2.IMREAD_GRAYSCALE)[90, 160]))
    rs = vp.extract_uniform_frames_ffmpeg(ramp, start=3.0, step=6.0, count=10, duration=60.0, out_dir=tmp / "o_ramp",
                                          cfg=Config(video_path=ramp, output_dir=tmp / "out2", frame_max_width=320))
    errs = [abs(float(np.interp(float(cv2.imread(str(x.path), cv2.IMREAD_GRAYSCALE)[110, 250]), xs, ts)) - x.timestamp_sec)
            for x in rs[1:]]                                   # bỏ khung đầu (t=3 nằm ngoài vùng hiệu chuẩn)
    check(f"THỜI ĐIỂM THẬT của khung khớp nhãn thời gian: lệch tối đa {max(errs):.2f}s (< 0.5s; lỗi cũ lệch +2.8s)", max(errs) < 0.5)

    # đường dự phòng OpenCV: chỉ 1 khung/cửa sổ (trước đây gấp đôi)
    v = tmp / "H.264.mp4"
    cap = cv2.VideoCapture(str(v))
    fps = cap.get(cv2.CAP_PROP_FPS); total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    windows = [(i * 6.0, (i + 1) * 6.0) for i in range(10)]
    s_old = vp._extract_windows_to_dir(cap, cv2, windows, tmp / "fb_old", cfg, fps, total)
    s_new = vp._extract_windows_to_dir(cap, cv2, windows, tmp / "fb_new", vp._OneFramePerWindowCfg(cfg), fps, total)
    cap.release()
    check(f"TÁI HIỆN lỗi cũ: cấu hình chung ra {len(s_old)} khung cho 10 cửa sổ 6s (gấp đôi)", len(s_old) == 20)
    check(f"đã sửa: đường dự phòng ra đúng {len(s_new)} khung", len(s_new) == 10)

    # FFmpeg lỗi (file hỏng) → trả None để người gọi dùng dự phòng, không ném lỗi
    bad = tmp / "bad.mp4"; bad.write_bytes(b"khong phai video")
    check("file hỏng → trả None (rơi về dự phòng), không sập",
          vp.extract_uniform_frames_ffmpeg(bad, start=1, step=2, count=3, duration=10, out_dir=tmp / "o_bad", cfg=cfg) is None)

print("TẤT CẢ PASS ✔")
