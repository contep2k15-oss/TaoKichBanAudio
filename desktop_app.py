"""desktop_app.py — điểm khởi chạy APP DESKTOP THẬT (không phải tab trình duyệt).

Chạy: python desktop_app.py  (chế độ phát triển, cần venv — xem README.md / GUI.md)
Khi đóng gói bằng PyInstaller (xem silent_video_voiceover_tool.spec), đây là entry point của file .exe.

Cách hoạt động: khởi chạy máy chủ Streamlit (app_streamlit.py) NGẦM trong một luồng nền, đợi nó sẵn sàng,
rồi mở một CỬA SỔ APP RIÊNG (bằng pywebview — dùng engine trình duyệt có sẵn của hệ điều hành, KHÔNG phải
mở tab Chrome/Edge) trỏ tới máy chủ đó. Với người dùng, trông y hệt một app desktop bình thường: 1 icon,
double-click, 1 cửa sổ riêng — không có thanh địa chỉ trình duyệt, không có tab.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("svvc.desktop")

APP_TITLE = "Silent Video Voiceover Creator"
STREAMLIT_SCRIPT = "app_streamlit.py"
HOST = "127.0.0.1"


def is_frozen() -> bool:
    """True khi đang chạy TỪ file .exe đã đóng gói bằng PyInstaller (không phải chạy source bằng `python`)."""
    return bool(getattr(sys, "frozen", False))


def app_base_dir() -> Path:
    """Thư mục chứa app_streamlit.py và tài nguyên khác — khác nhau giữa chế độ source và .exe đã đóng gói
    (PyInstaller giải nén tài nguyên vào một thư mục tạm, đường dẫn nằm ở `sys._MEIPASS`)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS"))  # noqa: B009 — chỉ tồn tại khi chạy từ .exe, cố ý dùng getattr
    return Path(__file__).resolve().parent


def _prepend_path(env_var: str, extra_dir: Path) -> None:
    if extra_dir.is_dir():
        os.environ[env_var] = str(extra_dir) + os.pathsep + os.environ.get(env_var, "")


def setup_bundled_binaries() -> None:
    """Khi đã đóng gói .exe: trỏ PATH tới ffmpeg/ffprobe đi kèm, và PLAYWRIGHT_BROWSERS_PATH tới Chromium đã
    đóng gói sẵn — để người tải file .exe KHÔNG cần tự cài FFmpeg hay chạy `playwright install` sau khi tải về.
    Chạy từ source (chưa đóng gói) thì bỏ qua — dùng FFmpeg/Chromium đã cài trên máy như bình thường."""
    if not is_frozen():
        return
    base = app_base_dir()
    _prepend_path("PATH", base / "ffmpeg_bin")
    browsers_dir = base / "playwright_browsers"
    if browsers_dir.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
        log.info("Dùng Chromium đóng gói sẵn: %s", browsers_dir)


def free_port() -> int:
    """Xin hệ điều hành cấp một cổng TCP còn trống — tránh xung đột nếu người dùng đã có Streamlit/app khác
    chạy sẵn ở cổng mặc định 8501."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def start_streamlit_server(port: int, script_path: str | None = None) -> None:
    """Chạy máy chủ Streamlit TRONG TIẾN TRÌNH HIỆN TẠI (không spawn subprocess `streamlit` — file .exe đóng
    gói không có lệnh `streamlit` độc lập để gọi ra ngoài, chỉ có thể gọi qua API Python nội bộ này). Hàm này
    CHẶN (blocking) — luôn gọi trong một thread riêng, không gọi ở luồng chính (luồng chính dành cho pywebview).

    Hai chỗ phải xử lý thủ công vì đang gọi thẳng API nội bộ thay vì qua lệnh `streamlit run` bình thường:
      1. `bootstrap.run()` cố cài signal handler (Ctrl+C) bằng `signal.signal()`, hàm này CHỈ chạy được ở
         luồng chính — ở đây ta cố tình chạy trong luồng nền nên phải vô hiệu hoá bước đó (không cần thiết:
         vòng đời app được điều khiển bằng việc đóng cửa sổ pywebview, không phải Ctrl+C trong terminal).
      2. Tham số cổng/host (`flag_options`) phải được nạp vào qua `load_config_options()` TRƯỚC khi gọi
         `bootstrap.run()`, nếu không Streamlit vẫn dùng cổng mặc định 8501 bất kể `flag_options` truyền vào.
    """
    from streamlit.web import bootstrap

    bootstrap._set_up_signal_handler = lambda server: None  # xem giải thích (1) ở trên

    script = script_path or str(app_base_dir() / STREAMLIT_SCRIPT)
    flag_options = {
        "server.port": port, "server.address": HOST, "server.headless": True,
        "browser.gatherUsageStats": False, "global.developmentMode": False,
        "server.fileWatcherType": "none",  # tắt auto-reload: không cần khi chạy đóng gói, đỡ tốn tài nguyên
    }
    bootstrap.load_config_options(flag_options)          # xem giải thích (2) ở trên — bắt buộc, nếu không cổng bị bỏ qua
    # LƯU Ý: KHÔNG dùng contextlib.redirect_stdout ở đây để ẩn banner của Streamlit — sys.stdout dùng chung
    # cho CẢ TIẾN TRÌNH (không phải riêng theo thread), nên redirect ở luồng nền này sẽ chặn im luôn mọi
    # print()/log của luồng chính (pywebview) trong suốt vòng đời app vì bootstrap.run() không bao giờ return.
    # Banner của Streamlit vô hại và không hiện ra với người dùng cuối (cửa sổ .exe đóng gói kiểu windowed
    # không có console để hiện banner đó), nên chấp nhận không ẩn thay vì phá vỡ toàn bộ output của app.
    bootstrap.run(script, False, [], flag_options)


def wait_for_server(port: int, timeout: float = 45.0) -> bool:
    """Chờ máy chủ Streamlit sẵn sàng nhận request, bằng cách hỏi endpoint health tiêu chuẩn của nó."""
    url = f"http://{HOST}:{port}/_stcore/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:  # noqa: S310 — URL cục bộ, cố định, không do người dùng nhập
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — máy chủ chưa kịp khởi động, thử lại tới khi hết thời gian chờ
            pass
        time.sleep(0.3)
    return False


def open_native_window(port: int) -> bool:
    """Thử mở CỬA SỔ APP RIÊNG bằng pywebview. Trả về True nếu thành công (hàm này BLOCK cho tới khi người
    dùng đóng cửa sổ). Trả về False nếu backend của pywebview không khởi động được (ví dụ máy Windows thiếu
    .NET Framework/WebView2 lành lặn — một lỗi tương tác giữa pywebview và PyInstaller khá phổ biến, KHÔNG
    liên quan tới bản thân ứng dụng), để `run()` tự chuyển sang phương án dự phòng thay vì crash cả app."""
    try:
        import webview
        icon = app_base_dir() / "assets" / "icon.png"
        webview.create_window(APP_TITLE, f"http://{HOST}:{port}", width=1320, height=880, min_size=(960, 640),
                              confirm_close=True)
        webview.start(icon=str(icon) if icon.is_file() else None)
        return True
    except Exception as e:  # noqa: BLE001 — lỗi khởi tạo GUI (thiếu .NET/WebView2, thiếu Qt/GTK...) không được phép crash app
        log.warning("Không mở được cửa sổ app riêng (%s: %s).", type(e).__name__, e)
        log.warning("Nguyên nhân thường gặp trên Windows: thiếu hoặc lỗi .NET Framework / Microsoft Edge "
                    "WebView2 Runtime trên máy này — cài WebView2 Runtime tại "
                    "https://developer.microsoft.com/microsoft-edge/webview2/ rồi thử chạy lại app để có "
                    "cửa sổ riêng; trong lúc chờ, app vẫn dùng được bình thường qua trình duyệt.")
        return False


def open_browser_fallback(port: int) -> int:
    """Phương án dự phòng khi không mở được cửa sổ app riêng: mở tab trình duyệt mặc định trỏ tới máy chủ
    (y hệt cách `Mo_Tool.bat` hoạt động) và giữ tiến trình sống tới khi người dùng tự đóng (Ctrl+C hoặc
    đóng cửa sổ terminal) — máy chủ Streamlit chạy ở luồng nền daemon nên thoát tiến trình là dừng hẳn."""
    import webbrowser
    url = f"http://{HOST}:{port}"
    log.info("Đang mở %s bằng trình duyệt mặc định...", url)
    webbrowser.open(url)
    log.info("App đang chạy trong tab trình duyệt vừa mở. GIỮ CỬA SỔ NÀY MỞ trong lúc dùng app — "
             "đóng cửa sổ này (hoặc Ctrl+C) sẽ tắt luôn app.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("Đã dừng theo yêu cầu.")
    return 0


def run(*, block: bool = True) -> int:
    """Điểm vào chính. `block=False` (dùng trong test) trả về ngay sau khi máy chủ sẵn sàng, KHÔNG mở cửa sổ
    pywebview (để có thể kiểm thử phần khởi động máy chủ mà không cần một môi trường đồ hoạ thật)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    from utils import suppress_console_windows
    suppress_console_windows()  # xem utils.py — bắt buộc để tiến trình con của pydub không tự bật console Windows
    setup_bundled_binaries()

    port = free_port()
    log.info("Khởi động máy chủ giao diện (cổng %d)...", port)
    t = threading.Thread(target=start_streamlit_server, args=(port,), daemon=True, name="streamlit-server")
    t.start()

    if not wait_for_server(port):
        log.error("Máy chủ giao diện không khởi động được sau 45s. Xem log phía trên để biết chi tiết lỗi "
                  "(thường là thiếu thư viện hoặc app_streamlit.py bị lỗi cú pháp).")
        return 1
    log.info("Sẵn sàng tại http://%s:%d", HOST, port)
    if not block:
        return 0

    if open_native_window(port):
        return 0
    return open_browser_fallback(port)


if __name__ == "__main__":
    sys.exit(run())
