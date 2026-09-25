"""python tests/test_youtube_downloader.py — mô phỏng yt_dlp, KHÔNG gọi mạng thật (sandbox không có quyền
truy cập youtube.com/googlevideo.com)."""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import youtube_downloader as yd  # noqa: E402
from config import Config  # noqa: E402
from utils import PipelineError  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


# ── validate_youtube_url: chỉ kiểm tra hình thức, không gọi mạng ──
for good in ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ",
            "http://youtube.com/watch?v=abc123", "https://www.youtube.com/shorts/abc123"]:
    check(f"URL hợp lệ được chấp nhận: {good}", yd.validate_youtube_url(good) == good.strip())
for bad in ["", "  ", "https://vimeo.com/12345", "khong phai link", "https://youtube.com/results?search_query=x"]:
    try:
        yd.validate_youtube_url(bad)
        raise AssertionError(f"phải từ chối: {bad!r}")
    except PipelineError:
        pass
print("  ✓ URL sai định dạng đều bị từ chối, không gọi mạng")


# ── probe_youtube / get_or_download: giả lập toàn bộ yt_dlp ──
class FakeYoutubeDL:
    calls: list[dict] = []
    fail_extract = False
    is_live = False

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        FakeYoutubeDL.calls.append({"action": "extract_info", "url": url, "download": download})
        if FakeYoutubeDL.fail_extract:
            raise RuntimeError("ERROR: Video unavailable")
        return {"id": "vid123", "title": "Video demo", "duration": 42.0, "is_live": FakeYoutubeDL.is_live}

    def download(self, urls):
        FakeYoutubeDL.calls.append({"action": "download", "urls": urls, "outtmpl": self.opts["outtmpl"]})
        # mô phỏng yt-dlp ghi ra file kết quả đúng theo outtmpl
        out = Path(self.opts["outtmpl"].replace("%(ext)s", "mp4"))
        out.write_bytes(b"FAKE_MP4_BYTES")


fake_module = types.SimpleNamespace(YoutubeDL=FakeYoutubeDL)
sys.modules["yt_dlp"] = fake_module

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg = Config(video_path=None, output_dir=tmp / "out")

    # 1) probe_youtube trả đúng thông tin, KHÔNG tải nội dung (download=False)
    info = yd.probe_youtube("https://youtu.be/dQw4w9WgXcQ")
    check("probe_youtube: đúng title/duration", info["title"] == "Video demo" and info["duration"] == 42.0)
    check("probe_youtube: KHÔNG tải nội dung (chỉ lấy metadata)", FakeYoutubeDL.calls[-1]["download"] is False)

    # 2) video đang LIVE → từ chối rõ ràng
    FakeYoutubeDL.is_live = True
    try:
        yd.probe_youtube("https://youtu.be/live123")
        raise AssertionError("phải từ chối video LIVE")
    except PipelineError as e:
        check(f"video LIVE bị từ chối rõ ràng: {e}", "LIVE" in str(e) or "trực tiếp" in str(e))
    FakeYoutubeDL.is_live = False

    # 3) lỗi mạng/video riêng tư → PipelineError rõ ràng, không crash kiểu lạ
    FakeYoutubeDL.fail_extract = True
    try:
        yd.probe_youtube("https://youtu.be/private123")
        raise AssertionError("phải báo lỗi")
    except PipelineError as e:
        check(f"lỗi trích xuất → PipelineError rõ ràng: {e}", "Video unavailable" in str(e))
    FakeYoutubeDL.fail_extract = False

    # 4) get_or_download: tải lần đầu, tạo đúng file .mp4
    FakeYoutubeDL.calls.clear()
    path1 = yd.get_or_download(cfg, "https://youtu.be/dQw4w9WgXcQ")
    check("get_or_download: file .mp4 được tạo ra", path1.is_file() and path1.suffix == ".mp4")
    check("get_or_download: nội dung đúng file vừa 'tải'", path1.read_bytes() == b"FAKE_MP4_BYTES")
    check("get_or_download: đã dọn sạch file tạm .download.*", not any(path1.parent.glob("*.download.*")))
    n_downloads_1 = sum(1 for c in FakeYoutubeDL.calls if c["action"] == "download")
    check("lần đầu: có gọi download() đúng 1 lần", n_downloads_1 == 1)

    # 5) gọi lại LẦN 2 với ĐÚNG url → dùng cache, KHÔNG tải lại
    FakeYoutubeDL.calls.clear()
    path2 = yd.get_or_download(cfg, "https://youtu.be/dQw4w9WgXcQ")
    check("lần 2 (cùng URL): trả về ĐÚNG file cũ, không tải lại", path2 == path1)
    n_downloads_2 = sum(1 for c in FakeYoutubeDL.calls if c["action"] == "download")
    check("lần 2: KHÔNG gọi download() (dùng cache)", n_downloads_2 == 0)

    # 6) đổi URL khác (video khác) → tải MỚI, không lẫn với cache cũ
    FakeYoutubeDL.calls.clear()

    def extract_info_v2(self, url, download=False):
        FakeYoutubeDL.calls.append({"action": "extract_info", "url": url, "download": download})
        return {"id": "vid456", "title": "Video khác", "duration": 99.0, "is_live": False}
    FakeYoutubeDL.extract_info = extract_info_v2
    path3 = yd.get_or_download(cfg, "https://youtu.be/anotherVideo1")
    check("URL khác → tải file MỚI, khác hẳn file cũ", path3 != path1 and path3.is_file())

print("TẤT CẢ PASS ✔")
