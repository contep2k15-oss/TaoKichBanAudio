"""Tự tạo icon app (assets/icon.ico) bằng Pillow — không cần thiết kế viên, không cần file ảnh có sẵn.

Chạy: python gen_icon.py
Dùng ở: video_translate_tool.spec (icon cho file .exe) và desktop_app.py (icon cửa sổ khi chạy).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICO_PATH = ASSETS_DIR / "icon.ico"
PNG_PATH = ASSETS_DIR / "icon.png"

BG_TOP = (91, 76, 235)      # tím-xanh, gợi nhắc "AI/voice"
BG_BOTTOM = (56, 45, 168)
MIC_COLOR = (255, 255, 255)
ACCENT = (255, 209, 102)    # sóng âm màu vàng nhấn


def _draw_mic(size: int) -> Image.Image:
    """Vẽ biểu tượng micro đơn giản trên nền gradient tròn, ở độ phân giải `size`x`size`."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for y in range(size):
        t = y / size
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg.paste(img, (0, 0), mask)
    img, draw = bg, ImageDraw.Draw(bg)

    cx, half_w = size / 2, size * 0.11
    cap_top, cap_bottom = size * 0.22, size * 0.56
    draw.rounded_rectangle((cx - half_w, cap_top, cx + half_w, cap_bottom), radius=half_w, fill=MIC_COLOR)

    stand_r = size * 0.24
    draw.arc((cx - stand_r, size * 0.30, cx + stand_r, cap_bottom + stand_r * 0.55), start=20, end=160,
             fill=MIC_COLOR, width=max(2, int(size * 0.045)))
    draw.line((cx, cap_bottom + stand_r * 0.30, cx, size * 0.78), fill=MIC_COLOR, width=max(2, int(size * 0.045)))
    draw.line((cx - size * 0.12, size * 0.78, cx + size * 0.12, size * 0.78), fill=MIC_COLOR, width=max(2, int(size * 0.045)))

    for i, dx in enumerate((-1, 1)):
        wr = size * (0.30 + i * 0.10)
        bbox = (cx - wr, cap_top + (cap_bottom - cap_top) * 0.15, cx + wr, cap_bottom - (cap_bottom - cap_top) * 0.05)
        draw.arc(bbox, start=290 if dx < 0 else -110, end=70 if dx < 0 else 250, fill=ACCENT, width=max(1, int(size * 0.02)))
    return img


def generate() -> Path:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    master = _draw_mic(256)
    master.save(PNG_PATH)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    master.save(ICO_PATH, format="ICO", sizes=[(s, s) for s in sizes])
    return ICO_PATH


if __name__ == "__main__":
    import sys

    # Console mặc định của Windows (kể cả trên máy ảo GitHub Actions) có thể dùng bảng mã cp1252, không hiểu
    # được chữ có dấu tiếng Việt và làm print() bên dưới bị crash (UnicodeEncodeError). Ép stdout sang UTF-8
    # trước khi in; nếu môi trường không cho phép đổi (hiếm), in bằng ASOI thuần thay vì crash cả bước build.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    path = generate()
    try:
        print(f"Đã tạo icon: {path} ({path.stat().st_size} bytes)")
    except UnicodeEncodeError:
        print(f"Da tao icon: {path} ({path.stat().st_size} bytes)")
