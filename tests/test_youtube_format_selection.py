"""python tests/test_youtube_format_selection.py — kiểm chứng chuỗi format bằng CHÍNH bộ chọn của yt-dlp
trên danh sách định dạng giả lập giống thật (không cần mạng). Chống tái phát lỗi "tải nhầm AV1 4K → giải mã
cực chậm" (đã đo: video 19 phút mất ~40 phút chỉ để quét khoảng lặng)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yt_dlp  # noqa: E402

from youtube_source import format_selector  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


def fmt(fid, ext, h=None, vcodec="none", acodec="none", tbr=1000):
    d = {"format_id": fid, "ext": ext, "vcodec": vcodec, "acodec": acodec, "tbr": tbr, "protocol": "https",
         "url": "http://x/" + fid, "format_note": ""}
    if h:
        d.update(height=h, width=int(h * 16 / 9))
    return d


FORMATS = [
    fmt("140", "m4a", acodec="mp4a.40.2", tbr=130), fmt("251", "webm", acodec="opus", tbr=140),
    fmt("136", "mp4", 720, "avc1.4d401f", tbr=1500), fmt("137", "mp4", 1080, "avc1.640028", tbr=4000),
    fmt("398", "mp4", 720, "av01.0.05M.08", tbr=900), fmt("399", "mp4", 1080, "av01.0.08M.08", tbr=1800),
    fmt("400", "mp4", 1440, "av01.0.12M.08", tbr=4500), fmt("401", "mp4", 2160, "av01.0.12M.08", tbr=9000),
    fmt("313", "webm", 2160, "vp09.00.50.08", tbr=15000),
]


def pick(selector: str, formats) -> list[str]:
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    sel = ydl.build_format_selector(selector)
    res = list(sel({"formats": formats, "has_merged_format": False, "incomplete_formats": False}))[0]
    return [f["format_id"] for f in res.get("requested_formats", [res])]


old = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"
check("TÁI HIỆN lỗi cũ: chuỗi không giới hạn chọn AV1 2160p (401)", pick(old, FORMATS)[0] == "401")

check("mặc định 1080p → H.264 1080p (137) + m4a (140), KHÔNG phải AV1", pick(format_selector(1080), FORMATS) == ["137", "140"])
check("720p → H.264 720p (136)", pick(format_selector(720), FORMATS) == ["136", "140"])

only_av1 = [f for f in FORMATS if not f["vcodec"].startswith("avc1")]
check("không có H.264: dự phòng chọn bản ≤1080p (399), không vượt độ cao", pick(format_selector(1080), only_av1)[0] == "399")

low = [fmt("140", "m4a", acodec="mp4a.40.2"), fmt("135", "mp4", 480, "avc1.4d401e")]
check("video chỉ có 480p vẫn tải được (không thất bại)", pick(format_selector(1080), low) == ["135", "140"])

check("người dùng CHỦ ĐỘNG chọn 2160p → thật sự lấy bản 4K (tôn trọng lựa chọn)", pick(format_selector(2160), FORMATS)[0] == "401")
check("chọn 1440p → lấy bản 1440p (400), không hạ xuống 1080p", pick(format_selector(1440), FORMATS)[0] == "400")

print("TẤT CẢ PASS ✔")
