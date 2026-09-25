# -*- mode: python ; coding: utf-8 -*-
"""Cấu hình PyInstaller — đóng gói toàn bộ dự án thành 1 thư mục .exe chạy độc lập (không cần cài Python).

Build: pyinstaller silent_video_voiceover_tool.spec
Kết quả: dist/SilentVideoVoiceoverCreator/SilentVideoVoiceoverCreator.exe (+ các file phụ trợ cùng thư mục).

LƯU Ý VỀ HAI ĐIỂM DỄ BỎ SÓT (đặc thù khi đóng gói app dùng Streamlit + Playwright):
  1. `app_streamlit.py` được Streamlit ĐỌC VÀ THỰC THI TRỰC TIẾP TỪ FILE (không phải import như module Python
     bình thường), nên phải: (a) liệt kê nó trong `Analysis(scripts=[...])` để PyInstaller quét ra hết các
     import nó cần (long_video_pipeline, config, checkpoint...) và đóng gói các module đó vào, VÀ (b) copy
     chính file .py đó vào bundle dưới dạng DỮ LIỆU THÔ (mục `datas`) để Streamlit đọc được nội dung lúc chạy.
  2. Playwright cần một "driver" (Node.js runtime riêng, nằm trong gói `playwright`) VÀ một bản trình duyệt
     Chromium đã tải riêng (không nằm trong gói `playwright`, nằm ở nơi `playwright install` tải về) — cả
     hai đều phải được gom vào `datas` thủ công, xem biến `PLAYWRIGHT_BROWSERS_SRC` bên dưới.
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None
PROJECT_DIR = os.path.abspath(os.path.dirname(SPEC))  # noqa: F821 — `SPEC` do PyInstaller tự cấp khi chạy spec file

# ════════════════════════════════════════════════════════════
# Dữ liệu cần gom kèm
# ════════════════════════════════════════════════════════════
datas = [
    (os.path.join(PROJECT_DIR, "app_streamlit.py"), "."),   # xem lưu ý (1)(b) ở trên — BẮT BUỘC, không phải tuỳ chọn
    (os.path.join(PROJECT_DIR, "assets"), "assets"),
]
datas += collect_data_files("streamlit")          # giao diện tĩnh (JS/CSS) của Streamlit
datas += collect_data_files("playwright")         # driver (Node.js runtime) của Playwright — KHÔNG phải trình duyệt
datas += copy_metadata("streamlit")               # streamlit tự kiểm tra version qua importlib.metadata lúc chạy
datas += copy_metadata("playwright")

# Trình duyệt Chromium thật (do `playwright install chromium` tải về, KHÔNG nằm trong gói pip `playwright`).
# Workflow GitHub Actions (.github/workflows/build.yml) chạy `playwright install chromium` rồi copy thư mục đó
# vào `PROJECT_DIR/playwright_browsers` TRƯỚC KHI gọi pyinstaller — nếu build tay ở máy local mà chưa làm
# bước copy đó, phần này bị bỏ qua và app đóng gói sẽ cần Chromium cài sẵn trên máy người dùng cuối.
_bundled_browsers = os.path.join(PROJECT_DIR, "playwright_browsers")
if os.path.isdir(_bundled_browsers):
    datas.append((_bundled_browsers, "playwright_browsers"))

# FFmpeg/ffprobe (do workflow tải sẵn và copy vào PROJECT_DIR/ffmpeg_bin trước khi build — xem build.yml).
_bundled_ffmpeg = os.path.join(PROJECT_DIR, "ffmpeg_bin")
if os.path.isdir(_bundled_ffmpeg):
    datas.append((_bundled_ffmpeg, "ffmpeg_bin"))

# ════════════════════════════════════════════════════════════
# Import mà PyInstaller khó tự dò ra (dynamic import, package có C-extension...)
# ════════════════════════════════════════════════════════════
hiddenimports = (
    collect_submodules("streamlit")
    + collect_submodules("playwright")
    + [
        "cv2", "numpy", "pydub", "pydub.silence", "pydub.generators",
        "edge_tts", "httpx", "google.genai", "google.genai.types",
        "engines.base", "engines.tts_edge", "engines.tts_elevenlabs",
        "engines.vision_gemini_web", "engines.vision_gemini_api",
        "webview", "webview.platforms.winforms", "webview.platforms.edgechromium",
    ]
)

a = Analysis(  # noqa: F821 — `Analysis`/`PYZ`/`EXE`/`COLLECT` do PyInstaller tự cấp khi chạy spec file
    ["desktop_app.py", "app_streamlit.py"],   # xem lưu ý (1)(a) — liệt kê CẢ HAI để quét đủ import
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test", "test", "tests"],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="SilentVideoVoiceoverCreator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX hay gây false-positive với antivirus trên file .exe đóng gói Python — tắt cho an toàn
    console=False,       # False = app cửa sổ (windowed), không hiện cửa sổ đen console
    icon=os.path.join(PROJECT_DIR, "assets", "icon.ico"),
)
coll = COLLECT(  # noqa: F821
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[], name="SilentVideoVoiceoverCreator",
)
