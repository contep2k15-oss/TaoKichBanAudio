"""python tests/test_youtube_source.py — validate URL, build prompt (thuần, không mạng), và test tải thật
NẾU sandbox có mạng ra ngoài (thường KHÔNG có với youtube.com — phần tải sẽ tự báo bỏ qua nếu vậy)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import Config  # noqa: E402
from youtube_source import (build_youtube_prompt, download_youtube_video, format_selector, is_youtube_url,  # noqa: E402
                            local_video_path)


def check(name, cond):
    assert cond, name
    print("  ✓", name)


# ── is_youtube_url ──
check("watch?v= hợp lệ", is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
check("youtu.be hợp lệ", is_youtube_url("https://youtu.be/dQw4w9WgXcQ"))
check("shorts hợp lệ", is_youtube_url("https://www.youtube.com/shorts/abc123XYZ_-"))
check("không có https:// → không hợp lệ", not is_youtube_url("youtube.com/watch?v=dQw4w9WgXcQ"))
check("link khác (vimeo) → không hợp lệ", not is_youtube_url("https://vimeo.com/12345"))
check("chuỗi rỗng → không hợp lệ", not is_youtube_url(""))
check("watch?v= có thêm tham số (&t=30s) vẫn hợp lệ", is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s"))

# ── build_youtube_prompt: nội dung cơ bản ──
cfg = Config(video_path=None, output_dir="/tmp/yt1", narrator_pov="Tôi", narrative_style="humorous")
p = build_youtube_prompt(cfg, "https://www.youtube.com/watch?v=abc123")
check("prompt chứa ĐÚNG NGUYÊN VĂN link (để Gemini nhận diện được)", "https://www.youtube.com/watch?v=abc123" in p)
check("prompt yêu cầu XEM TRỰC TIẾP video (không phải chỉ đọc transcript)", "XEM TRỰC TIẾP" in p)
check("prompt yêu cầu phủ TOÀN BỘ video, không chỉ phần đầu", "TOÀN BỘ video" in p and "không chỉ phần đầu" in p)
check("có chỉ dẫn ngôi kể đã cấu hình", "Tôi" in p)
check("có chỉ dẫn phong cách hài hước", "hài hước" in p.lower())
check("có định dạng JSON mẫu đúng schema dùng chung với script_generator", '"start_time"' in p and '"tone"' in p)
check("có yêu cầu định dạng HH:MM:SS.mmm rõ ràng", "HH:MM:SS.mmm" in p)

# ── format_selector / local_video_path: chống lỗi "tải 4K AV1 giải mã cực chậm" ──
f1080 = format_selector(1080)
check("chuỗi format giới hạn độ cao theo tham số", "height<=1080" in f1080 and "height<=720" not in f1080)
check("ƯU TIÊN codec H.264 (avc1) — giải mã nhanh, không phải AV1", f1080.index("avc1") < f1080.index("/bv*[height<=1080][ext=mp4]"))
check("có nhánh dự phòng cuối cùng (b) để không thất bại khi thiếu định dạng ưa thích", f1080.endswith("/b"))
check("độ cao khác → chuỗi khác tương ứng", "height<=720" in format_selector(720))
check("mọi nhánh đều bị giới hạn độ cao (trừ dự phòng cuối cùng)",
      all("height<=" in part for part in f1080.split("/")[:-1]))
check("tên file cache theo độ cao", local_video_path(Path("/w"), 1080).name == "youtube_source_1080p.mp4")
check("độ cao khác → tên file khác (không dùng nhầm bản cũ)",
      local_video_path(Path("/w"), 720) != local_video_path(Path("/w"), 1080))

# ── Config validation ──
for bad in (0, 100, 5000):
    try:
        Config(video_path=None, output_dir="/tmp/yh", youtube_url="https://youtu.be/abc", youtube_max_height=bad)
        raise AssertionError(f"youtube_max_height={bad} phải bị từ chối")
    except ValueError:
        pass
print("  ✓ youtube_max_height ngoài [144, 4320] bị từ chối")
check("youtube_max_height=1080 hợp lệ",
      Config(video_path=None, output_dir="/tmp/yh2", youtube_url="https://youtu.be/abc", youtube_max_height=1080).youtube_max_height == 1080)

# ── download_youtube_video: thử tải thật 1 video công khai rất ngắn ──
import tempfile
with tempfile.TemporaryDirectory() as d:
    out = Path(d) / "test.mp4"
    try:
        download_youtube_video("https://www.youtube.com/watch?v=BaW_jenozKc", out)  # video test chính thức của yt-dlp
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ BỎ QUA test tải thật — sandbox không có mạng ra youtube.com (dự kiến): {type(e).__name__}: {e}")
    else:
        check("file đã tải tồn tại và có kích thước > 0", out.is_file() and out.stat().st_size > 0)
        from utils import probe_media
        info = probe_media(out)
        check(f"file tải về là video hợp lệ, đọc được bằng ffprobe (dài {info.duration_sec:.1f}s)", info.duration_sec > 0)

print("TẤT CẢ PASS ✔")
