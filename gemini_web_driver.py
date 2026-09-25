"""Option B — điều khiển giao diện Gemini Web bằng Playwright + Persistent Context.

Ý tưởng chính
  • Một thư mục profile duy nhất (`user_data_dir`) lưu cookie/phiên Google. Người dùng đăng nhập TAY 1 lần trong
    một cửa sổ Chrome bình thường (không bị điều khiển tự động → Google cho đăng nhập bình thường); các lần sau
    Playwright mở lại đúng profile đó (mặc định headless) và tự gửi prompt + ảnh.
  • Mỗi prompt dùng một cuộc trò chuyện MỚI (tránh nhồi ngữ cảnh/ảnh cũ); ngữ cảnh liên tục được nhắc lại trong prompt.
  • Toàn bộ selector nằm trong `Selectors` (có nhiều phương án dự phòng) và có thể ghi đè bằng --selectors-file,
    vì giao diện web của Gemini thay đổi thường xuyên.
  • Mọi phản hồi đều đi qua `response_parser.analyze_response()` để phân biệt JSON / HTML / lỗi mạng / đổi DOM...
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Sequence

from config import Config
from response_parser import ResponseKind, analyze_response
from utils import GeminiWebError, PipelineError, WebAuthError, WebDOMError, WebNetworkError

try:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:  # --engine api không cần Playwright
    PWError = PWTimeout = Exception  # type: ignore[misc,assignment]
    sync_playwright = None  # type: ignore[assignment]

log = logging.getLogger("svvc.web")

STRICT_SUFFIX = ("\n\nLƯU Ý QUAN TRỌNG: chỉ trả về DUY NHẤT một khối mã ```json ... ``` chứa mảng JSON hợp lệ theo đúng mẫu, "
                 "không thêm lời dẫn hay giải thích nào khác.")


# ════════════════════════════════════════════════════════════
# Selector (có phương án dự phòng; người dùng có thể ghi đè bằng JSON)
# ════════════════════════════════════════════════════════════
@dataclass
class Selectors:
    prompt_box: list[str] = field(default_factory=lambda: [
        "rich-textarea div.ql-editor[contenteditable='true']", "div.ql-editor[contenteditable='true']",
        "div[role='textbox'][contenteditable='true']", "div[contenteditable='true'][aria-label*='prompt' i]"])
    send_button: list[str] = field(default_factory=lambda: [
        "button.send-button", "button[aria-label='Send message']", "button[aria-label*='Send' i]",
        "button[aria-label*='Gửi' i]"])
    stop_button: list[str] = field(default_factory=lambda: [
        "button.stop", "button[aria-label='Stop response']", "button[aria-label*='Stop' i]", "button[aria-label*='Dừng' i]"])
    upload_menu_button: list[str] = field(default_factory=lambda: [
        "button[aria-label='Open upload file menu']", "button[aria-label*='upload' i]", "button[aria-label*='Tải' i]",
        "button.upload-card-button", "button[aria-label='Add files']", "button[aria-label*='Add' i][aria-haspopup]"])
    upload_files_item: list[str] = field(default_factory=lambda: [
        "button[data-test-id='local-images-files-uploader-button']", "[role='menuitem']:has-text('Upload files')",
        "button:has-text('Upload files')", "[role='menuitem']:has-text('Upload')", "[role='menuitem']:has-text('Tải tệp')"])
    file_input: list[str] = field(default_factory=lambda: ["input[type='file']"])
    file_previews: list[str] = field(default_factory=lambda: [
        "uploader-file-preview", "[data-test-id='file-preview']", "div.file-preview-container img",
        "button[aria-label^='Remove file' i]", "button[aria-label^='Xóa tệp' i]"])
    response_container: list[str] = field(default_factory=lambda: [
        "model-response", "div.response-container", "[data-test-id='model-response']", "message-content"])
    code_blocks: list[str] = field(default_factory=lambda: ["code-block code", "pre code"])
    error_banner: list[str] = field(default_factory=lambda: [
        "error-message", "[data-test-id='error-message']", "div.error-message", "snack-bar-container", "simple-snack-bar"])
    model_picker: list[str] = field(default_factory=lambda: [
        "button[data-test-id='bard-mode-menu-button']", "button.input-area-switch", "button[aria-label*='mode picker' i]",
        "button[aria-label*='model' i]"])
    model_options: list[str] = field(default_factory=lambda: [
        "button[role='menuitemradio']", "[role='menuitemradio']", "button.bard-mode-list-button", "[role='menuitem']"])
    signin_link: list[str] = field(default_factory=lambda: [
        "a[href^='https://accounts.google.com/ServiceLogin']", "a[href*='accounts.google.com']:has-text('Sign in')",
        "a:has-text('Đăng nhập')"])

    @classmethod
    def load(cls, path: Path | None) -> "Selectors":
        sel = cls()
        if path:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as e:
                raise PipelineError(f"Không đọc được selectors file {path}: {e}") from e
            valid = {f.name for f in fields(cls)}
            for name, values in data.items():
                if name not in valid:
                    raise PipelineError(f"selectors file: khoá lạ '{name}'. Hợp lệ: {', '.join(sorted(valid))}")
                values = [values] if isinstance(values, str) else list(values)
                setattr(sel, name, values + getattr(sel, name))      # selector của người dùng được thử TRƯỚC
            log.info("Đã nạp selector tuỳ chỉnh từ %s (%d nhóm).", path, len(data))
        return sel


@dataclass
class ResponseSnapshot:
    text: str = ""
    code_texts: list[str] = field(default_factory=list)
    html_len: int = 0
    count: int = 0
    generating: bool = False
    banner: str = ""


# ════════════════════════════════════════════════════════════
# Đăng nhập tay 1 lần (trong Chrome bình thường, KHÔNG bị điều khiển tự động)
# ════════════════════════════════════════════════════════════
def find_browser_executable(channel: str) -> str | None:
    env = os.getenv("SVVC_BROWSER_PATH")
    if env and Path(env).exists():
        return env
    if channel == "chromium":
        if sync_playwright is None:
            return None
        with sync_playwright() as p:
            path = p.chromium.executable_path
        return path if Path(path).exists() else None

    if sys.platform == "win32":
        roots = [os.getenv(k) for k in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")]
        rel = {"chrome": r"Google\Chrome\Application\chrome.exe", "msedge": r"Microsoft\Edge\Application\msedge.exe"}[channel]
        cands = [Path(r) / rel for r in roots if r]
    elif sys.platform == "darwin":
        cands = [Path({"chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "msedge": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"}[channel])]
    else:
        names = {"chrome": ["google-chrome", "google-chrome-stable", "chrome"], "msedge": ["microsoft-edge", "msedge"]}[channel]
        cands = [Path(p) for n in names if (p := shutil.which(n))]
    return next((str(c) for c in cands if c.exists()), None)


def interactive_login(cfg: Config, input_fn=input) -> bool:
    """Mở Chrome thật với profile dùng chung để người dùng tự đăng nhập; trả về True nếu phiên hợp lệ."""
    exe = find_browser_executable(cfg.browser_channel)
    if not exe:
        raise GeminiWebError(
            f"Không tìm thấy trình duyệt '{cfg.browser_channel}'. Cài Google Chrome (hoặc dùng --browser msedge / "
            "--browser chromium sau khi chạy `playwright install chromium`; hoặc đặt SVVC_BROWSER_PATH).")
    cfg.user_data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, f"--user-data-dir={cfg.user_data_dir}", "--no-first-run", "--no-default-browser-check",
           "--password-store=basic", cfg.gemini_url]
    print("\n" + "═" * 64 +
          f"\n ĐĂNG NHẬP GEMINI (một lần duy nhất)\n Profile: {cfg.user_data_dir}\n"
          " 1) Một cửa sổ Chrome sắp mở — hãy đăng nhập tài khoản Google của bạn.\n"
          " 2) Khi đã thấy giao diện chat của Gemini, quay lại đây và nhấn Enter (Đã đăng nhập).\n"
          " 3) Sau đó ĐÓNG cửa sổ Chrome đó để phiên được lưu.\n" + "═" * 64)
    proc = subprocess.Popen(cmd)
    try:
        input_fn("\n▶ Nhấn Enter khi bạn ĐÃ ĐĂNG NHẬP xong... ")
        deadline = time.monotonic() + 120
        while proc.poll() is None and time.monotonic() < deadline:
            print("  … hãy đóng cửa sổ Chrome đăng nhập để lưu phiên (đang chờ)", end="\r")
            time.sleep(1.0)
        if proc.poll() is None:
            log.warning("Cửa sổ Chrome vẫn mở sau 120s → đóng giúp bạn.")
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    except (KeyboardInterrupt, EOFError):
        proc.terminate()
        raise
    print("\nĐang kiểm tra phiên đăng nhập (headless)...")
    ok = check_session(cfg)
    print("✔ Phiên đăng nhập hợp lệ — các lần sau không cần đăng nhập lại." if ok else
          "✖ Chưa thấy phiên đăng nhập hợp lệ. Hãy chạy lại --login và chắc chắn đã đăng nhập xong rồi mới đóng cửa sổ.")
    return ok


def check_session(cfg: Config, headless: bool = True) -> bool:
    with GeminiWebDriver(cfg, headless=headless) as drv:
        return drv.is_logged_in()


# ════════════════════════════════════════════════════════════
# Driver
# ════════════════════════════════════════════════════════════
class GeminiWebDriver:
    def __init__(self, cfg: Config, selectors: Selectors | None = None, headless: bool | None = None) -> None:
        self.cfg = cfg
        self.S = selectors or Selectors.load(cfg.selectors_file)
        self.headless = cfg.headless if headless is None else headless
        self.debug_dir = cfg.web_debug_dir
        self._pw: Any = None
        self.ctx: Any = None
        self.page: Any = None
        self._last_prompt_at = 0.0

    # ── vòng đời ──────────────────────────────────────────
    def __enter__(self) -> "GeminiWebDriver":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> None:
        if sync_playwright is None:
            raise PipelineError("Chưa cài Playwright: pip install playwright (rồi `playwright install chromium` nếu dùng --browser chromium)")
        self.cfg.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs: dict[str, Any] = dict(user_data_dir=str(self.cfg.user_data_dir), headless=self.headless,
                                      viewport={"width": 1280, "height": 900}, locale="en-US", accept_downloads=False)
        if self.cfg.browser_channel != "chromium":
            kwargs["channel"] = self.cfg.browser_channel
        log.info("Mở trình duyệt (%s, %s) với profile %s", self.cfg.browser_channel,
                 "headless" if self.headless else "có giao diện", self.cfg.user_data_dir)
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:  # noqa: BLE001
            self._stop_pw()
            raise GeminiWebError(self._launch_hint(str(e))) from e
        self.ctx.set_default_timeout(30_000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def close(self) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        except Exception as e:  # noqa: BLE001
            log.debug("Đóng context lỗi: %s", e)
        self.ctx = self.page = None
        self._stop_pw()

    def _stop_pw(self) -> None:
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = None

    @staticmethod
    def _launch_hint(msg: str) -> str:
        low = msg.lower()
        if any(s in low for s in ("processsingleton", "existing browser session", "already in use", "profile appears to be in use")):
            return ("Profile trình duyệt đang được một cửa sổ Chrome khác sử dụng. Hãy đóng cửa sổ Chrome dùng "
                    "profile này (kể cả cửa sổ đăng nhập) hoặc dừng tiến trình chạy song song, rồi thử lại.")
        if "executable doesn't exist" in low or "distribution" in low or "is not found" in low:
            return ("Không khởi chạy được trình duyệt. Cài Google Chrome, hoặc chạy `playwright install chrome`, "
                    "hoặc dùng --browser chromium sau khi `playwright install chromium`.\nChi tiết: " + msg.splitlines()[0])
        return "Không khởi chạy được trình duyệt: " + msg.splitlines()[0]

    # ── helper tìm phần tử ────────────────────────────────
    def _first_match(self, group: Sequence[str], timeout_ms: int = 0) -> tuple[Any, str | None]:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for sel in group:
                try:
                    loc = self.page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return loc.first, sel
                except PWError:
                    continue
            if time.monotonic() >= deadline:
                return None, None
            self.page.wait_for_timeout(250)

    def _first_visible(self, group: Sequence[str], timeout_ms: int = 0) -> Any:
        return self._first_match(group, timeout_ms)[0]

    # ── điều hướng / đăng nhập ────────────────────────────
    def goto_app(self) -> None:
        try:
            self.page.goto(self.cfg.gemini_url, wait_until="domcontentloaded", timeout=45_000)
        except PWTimeout as e:
            raise WebNetworkError("Hết thời gian mở gemini.google.com (mạng chậm/bị chặn).") from e
        except PWError as e:
            if "net::ERR" in str(e):
                raise WebNetworkError(f"Không truy cập được gemini.google.com: {str(e).splitlines()[0]}") from e
            raise

    def is_logged_in(self) -> bool:
        self.goto_app()
        if "accounts.google.com" in self.page.url:
            return False
        if not self._first_visible(self.S.prompt_box, 25_000):
            return False
        self.page.wait_for_timeout(1200)          # chờ nút "Sign in" (nếu là khách) kịp hiện
        return self._first_visible(self.S.signin_link, 1500) is None and "accounts.google.com" not in self.page.url

    def ensure_logged_in(self) -> None:
        if not self.is_logged_in():
            self.dump_debug("not_logged_in")
            raise WebAuthError("Chưa đăng nhập Gemini (hoặc phiên đã hết hạn). Chạy `python main.py --login` để đăng nhập tay một lần.")

    def _raise_if_login_page(self) -> None:
        if "accounts.google.com" in (self.page.url or ""):
            raise WebAuthError("Phiên Google hết hạn (bị chuyển tới trang đăng nhập). Chạy `python main.py --login`.")

    def _dismiss_dialogs(self) -> None:
        pattern = re.compile(r"^(got it|not now|no thanks|dismiss|close|i agree|accept all|maybe later|skip|continue|"
                             r"đã hiểu|không, cảm ơn|để sau|bỏ qua|đóng|chấp nhận tất cả)$", re.I)
        try:
            dialogs = self.page.locator("[role='dialog'], mat-dialog-container")
            if dialogs.count() == 0:
                return
            buttons = dialogs.first.get_by_role("button")
            for i in range(min(buttons.count(), 8)):
                label = ((buttons.nth(i).inner_text() or "") or (buttons.nth(i).get_attribute("aria-label") or "")).strip()
                if pattern.match(label):
                    log.info("Đóng hộp thoại: “%s”", label)
                    buttons.nth(i).click(timeout=2000)
                    return
        except PWError as e:
            log.debug("Bỏ qua lỗi khi đóng hộp thoại: %s", e)

    def select_model(self, hint_regex: str) -> bool:
        """Chọn model theo regex (vd 'Flash|Fast'). Không tìm thấy → cảnh báo và dùng model mặc định."""
        picker = self._first_visible(self.S.model_picker, 3000)
        if picker is None:
            log.warning("Không thấy nút chọn model (selector model_picker) — dùng model mặc định của giao diện.")
            return False
        rx = re.compile(hint_regex, re.I)
        try:
            current = (picker.inner_text() or "") + " " + (picker.get_attribute("aria-label") or "")
            if rx.search(current):
                log.info("Model hiện tại đã khớp “%s”: %s", hint_regex, " ".join(current.split())[:60])
                return True
            picker.click()
            self.page.wait_for_timeout(500)
            options = self.page.locator(",".join(self.S.model_options))
            for i in range(min(options.count(), 12)):
                text = options.nth(i).inner_text() or ""
                if rx.search(text):
                    options.nth(i).click()
                    log.info("Đã chọn model: %s", " ".join(text.split())[:60])
                    self.page.wait_for_timeout(500)
                    return True
            self.page.keyboard.press("Escape")
        except PWError as e:
            log.debug("select_model lỗi: %s", e)
        log.warning("Không tìm thấy model khớp “%s” trong menu — dùng model mặc định.", hint_regex)
        return False

    def new_chat(self) -> None:
        self.goto_app()
        self._raise_if_login_page()
        if not self._first_visible(self.S.prompt_box, 30_000):
            self.dump_debug("no_prompt_box")
            self._raise_if_login_page()
            raise WebDOMError("Không thấy ô nhập prompt (selector prompt_box). Gemini có thể đã đổi giao diện hoặc trang chưa tải xong. "
                              "Chạy `python main.py --check-web --headed` và xem web_debug/.")
        self._dismiss_dialogs()
        if self.cfg.web_model_hint:
            self.select_model(self.cfg.web_model_hint)

    # ── đính kèm ảnh ──────────────────────────────────────
    def _count_previews(self) -> int:
        best = 0
        for sel in self.S.file_previews:
            try:
                best = max(best, self.page.locator(sel).count())
            except PWError:
                pass
        return best

    def _send_enabled(self) -> bool:
        btn = self._first_visible(self.S.send_button, 0)
        try:
            return bool(btn) and btn.is_enabled() and btn.get_attribute("aria-disabled") != "true"
        except PWError:
            return False

    def attach_images(self, paths: Sequence[Path]) -> None:
        files = [str(Path(p).resolve()) for p in paths]
        if not files:
            return
        timeout = self.cfg.web_upload_timeout_sec
        log.info("Đính kèm %d ảnh...", len(files))
        attached = False
        # Cách 1: <input type=file> có sẵn trong DOM
        try:
            inp = self.page.locator(",".join(self.S.file_input))
            if inp.count() > 0:
                inp.first.set_input_files(files)
                self.page.wait_for_timeout(1500)
                attached = self._count_previews() > 0
        except PWError as e:
            log.debug("set_input_files trực tiếp không được: %s", e)
        # Cách 2: bấm nút "+" → "Upload files" và bắt file chooser
        if not attached:
            attached = self._attach_via_chooser(files)
        # Chờ thumbnail + nút Gửi sẵn sàng (nút Gửi bị vô hiệu khi đang upload)
        deadline = time.monotonic() + timeout
        seen_preview = False
        while time.monotonic() < deadline:
            cnt = self._count_previews()
            seen_preview = seen_preview or cnt > 0
            if cnt >= len(files):
                break
            if not seen_preview and time.monotonic() - (deadline - timeout) > 6:
                break
            self.page.wait_for_timeout(400)
        if not seen_preview:
            wait = min(2.0 + 1.5 * len(files), timeout)
            log.warning("Không đếm được thumbnail (selector file_previews có thể lỗi thời) → chờ cố định %.0fs.", wait)
            self.page.wait_for_timeout(int(wait * 1000))
        elif self._count_previews() < len(files):
            log.warning("Chỉ thấy %d/%d thumbnail sau %.0fs.", self._count_previews(), len(files), timeout)

    def _attach_via_chooser(self, files: list[str]) -> bool:
        menu = self._first_visible(self.S.upload_menu_button, 8000)
        if menu is None:
            self.dump_debug("no_upload_button")
            raise WebDOMError("Không thấy nút tải tệp (selector upload_menu_button). Gemini có thể đã đổi giao diện, "
                              "hoặc tài khoản/vùng của bạn không cho tải ảnh lên. Chạy `--check-web --headed`.")
        try:      # UI cũ: bấm "+" mở thẳng hộp chọn tệp
            with self.page.expect_file_chooser(timeout=2500) as fc:
                menu.click()
            fc.value.set_files(files)
            return True
        except PWTimeout:
            pass
        item = self._first_visible(self.S.upload_files_item, 5000)   # UI mới: menu → "Upload files"
        if item is None:
            self.dump_debug("no_upload_item")
            raise WebDOMError("Đã mở menu tải lên nhưng không thấy mục 'Upload files' (selector upload_files_item).")
        with self.page.expect_file_chooser(timeout=10_000) as fc:
            item.click()
        fc.value.set_files(files)
        return True

    # ── gửi prompt ────────────────────────────────────────
    def send_prompt(self, text: str) -> None:
        box = self._first_visible(self.S.prompt_box, 10_000)
        if box is None:
            raise WebDOMError("Mất ô nhập prompt khi gửi (selector prompt_box).")
        box.click()
        box.fill(text)
        if not (box.inner_text() or "").strip():
            self.page.keyboard.insert_text(text)        # dự phòng nếu fill() không tác dụng với editor
        deadline = time.monotonic() + self.cfg.web_upload_timeout_sec
        while time.monotonic() < deadline and not self._send_enabled():
            self.page.wait_for_timeout(400)
        btn = self._first_visible(self.S.send_button, 0)
        self._last_prompt_at = time.monotonic()
        if btn is not None and self._send_enabled():
            btn.click()
        else:
            log.warning("Nút Gửi không khả dụng/không tìm thấy → gửi bằng phím Enter.")
            box.press("Enter")

    # ── chờ & đọc phản hồi ────────────────────────────────
    def _response_locator(self) -> tuple[Any, int]:
        for sel in self.S.response_container:
            try:
                loc = self.page.locator(sel)
                n = loc.count()
            except PWError:
                continue
            if n > 0:
                return loc, n
        return None, 0

    def _response_count(self) -> int:
        return self._response_locator()[1]

    def _is_generating(self) -> bool:
        return self._first_visible(self.S.stop_button, 0) is not None

    def _snapshot(self) -> ResponseSnapshot:
        snap = ResponseSnapshot(generating=self._is_generating())
        try:
            banner = self._first_visible(self.S.error_banner, 0)
            if banner is not None:
                snap.banner = " ".join((banner.inner_text() or "").split())[:300]
        except PWError:
            pass
        loc, n = self._response_locator()
        snap.count = n
        if loc is None:
            return snap
        try:
            last = loc.nth(n - 1)
            snap.text = last.inner_text(timeout=3000) or ""
            snap.code_texts = [t for t in last.locator(",".join(self.S.code_blocks)).all_inner_texts() if t.strip()]
            snap.html_len = len(last.inner_html(timeout=3000) or "")
        except PWError as e:
            log.debug("Đọc phản hồi lỗi tạm thời: %s", e)
        return snap

    def wait_for_response(self, n_before: int) -> tuple[ResponseSnapshot, bool]:
        """Trả về (snapshot cuối cùng, timed_out)."""
        timeout = self.cfg.web_response_timeout_sec
        start = time.monotonic()
        snap = ResponseSnapshot()
        while True:                                       # giai đoạn 1: phần tử phản hồi mới xuất hiện
            snap = self._snapshot()
            if snap.count > n_before:
                break
            self._raise_if_login_page()
            if snap.banner:
                return snap, False
            if time.monotonic() - start > min(60.0, timeout):
                return snap, True
            self.page.wait_for_timeout(500)

        last_text, stable_at = "", time.monotonic()       # giai đoạn 2: đợi nội dung ổn định & dừng sinh
        while time.monotonic() - start < timeout:
            snap = self._snapshot()
            now = time.monotonic()
            if snap.text != last_text or snap.generating:
                last_text, stable_at = snap.text, now
            elif snap.text.strip() and now - stable_at >= self.cfg.web_stable_sec:
                return snap, False
            elif snap.banner and not snap.text.strip():
                return snap, False
            self._raise_if_login_page()
            self.page.wait_for_timeout(700)
        return snap, True

    # ── API mức cao ───────────────────────────────────────
    def _polite_delay(self) -> None:
        wait = self.cfg.web_delay_between_prompts_sec + random.uniform(0, 1.5) - (time.monotonic() - self._last_prompt_at)
        if self._last_prompt_at and wait > 0:
            time.sleep(wait)

    def ask(self, prompt: str, images: Sequence[Path] = ()) -> tuple[list[str], dict[str, Any], ResponseSnapshot]:
        """Một lượt hỏi trong cuộc trò chuyện mới. Trả về (các bản văn ứng viên, signals, snapshot)."""
        self._polite_delay()
        self.new_chat()
        if images:
            self.attach_images(images)
        n_before = self._response_count()
        self.send_prompt(prompt)
        snap, timed_out = self.wait_for_response(n_before)
        candidates = list(snap.code_texts) + ([snap.text] if snap.text.strip() else [])
        if not candidates and snap.banner:
            candidates = [snap.banner]
        try:
            online = bool(self.page.evaluate("navigator.onLine"))
        except PWError:
            online = True
        signals = {"response_count": snap.count, "timed_out": timed_out, "generating": snap.generating,
                   "response_html_len": snap.html_len, "online": online}
        return candidates, signals, snap

    def ask_json(self, prompt: str, images: Sequence[Path] = (), *, label: str = "ask") -> list[dict]:
        """Gửi prompt (+ ảnh) và trả về mảng JSON đã bóc. Phân loại lỗi chi tiết + thử lại thông minh."""
        best_partial: list[dict] | None = None
        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.web_retries + 1):
            try:
                cands, signals, _snap = self.ask(prompt + (STRICT_SUFFIX if attempt > 1 else ""), images)
            except (WebAuthError, WebDOMError):
                raise
            except WebNetworkError as e:
                last_exc = e
                log.warning("[%s] Lỗi mạng (lần %d/%d): %s", label, attempt, self.cfg.web_retries, e)
                self._backoff(attempt)
                continue
            except PWTimeout as e:
                last_exc = WebNetworkError(f"Hết thời gian thao tác với trang: {str(e).splitlines()[0]}")
                log.warning("[%s] Timeout Playwright (lần %d/%d): %s", label, attempt, self.cfg.web_retries, last_exc)
                self.dump_debug(f"{label}_a{attempt}_timeout")
                self._backoff(attempt)
                continue
            except PWError as e:
                msg = str(e)
                if "has been closed" in msg or "Target closed" in msg:
                    raise GeminiWebError("Trình duyệt đã bị đóng giữa chừng.") from e
                last_exc = WebNetworkError(msg.splitlines()[0]) if "net::ERR" in msg else GeminiWebError(msg.splitlines()[0])
                log.warning("[%s] Lỗi Playwright (lần %d/%d): %s", label, attempt, self.cfg.web_retries, last_exc)
                self.dump_debug(f"{label}_a{attempt}_pwerror")
                self._backoff(attempt)
                continue

            payload, diag = analyze_response(cands, page_url=self.page.url, signals=signals)
            if diag.ok and payload is not None:
                log.info("[%s] %s", label, diag.message)
                return payload
            level = logging.WARNING
            log.log(level, "[%s] ⚠ %s [%s]", label, diag.message, diag.kind.value)
            if diag.hint:
                log.log(level, "[%s]   → %s", label, diag.hint)
            self.dump_debug(f"{label}_a{attempt}_{diag.kind.value}", cands)
            last_exc = diag.to_exception()
            if diag.kind is ResponseKind.TRUNCATED_JSON and payload:
                best_partial = payload if best_partial is None or len(payload) > len(best_partial) else best_partial
            if not diag.retryable:
                raise last_exc
            self._backoff(attempt)

        if best_partial:
            log.warning("[%s] Dùng %d phần tử JSON khôi phục được từ phản hồi bị cắt cụt.", label, len(best_partial))
            return best_partial
        raise last_exc or GeminiWebError(f"[{label}] Thất bại không rõ nguyên nhân")

    def _backoff(self, attempt: int) -> None:
        if attempt >= self.cfg.web_retries:
            return
        wait = min(60.0, 5.0 * 2 ** (attempt - 1)) + random.uniform(0, 2)
        log.info("Chờ %.0fs rồi thử lại bằng cuộc trò chuyện mới...", wait)
        time.sleep(wait)

    # ── debug ─────────────────────────────────────────────
    def dump_debug(self, name: str, texts: Sequence[str] = ()) -> None:
        """Lưu screenshot + HTML trang + văn bản đã cào để dễ chẩn đoán khi Gemini đổi giao diện."""
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S")
            base = self.debug_dir / f"{stamp}_{re.sub(r'[^\w.-]+', '_', name)}"
            if self.page:
                self.page.screenshot(path=str(base) + ".png")
                Path(str(base) + ".html").write_text(self.page.content(), encoding="utf-8")
            if texts:
                Path(str(base) + ".txt").write_text("\n\n=====\n\n".join(texts), encoding="utf-8")
            log.info("Đã lưu dữ liệu chẩn đoán: %s.*", base)
        except Exception as e:  # noqa: BLE001
            log.debug("dump_debug lỗi: %s", e)

    # ── tự kiểm tra (--check-web) ─────────────────────────
    def selfcheck(self, test_upload: Path | None = None, test_prompt: bool = True) -> list[tuple[str, bool, str]]:
        rows: list[tuple[str, bool, str]] = []
        logged = self.is_logged_in()
        rows.append(("Đăng nhập", logged, self.page.url[:70]))
        if not logged:
            self.dump_debug("check_not_logged_in")
            return rows
        self.new_chat()
        for name in ("prompt_box", "upload_menu_button", "model_picker"):
            _, sel = self._first_match(getattr(self.S, name), 2500)
            rows.append((f"selector {name}", sel is not None, sel or "KHÔNG TÌM THẤY"))
        if test_upload is not None:
            try:
                self.attach_images([test_upload])
                n = self._count_previews()
                rows.append(("tải ảnh lên + thumbnail (file_previews)", n >= 1, f"{n} thumbnail"))
            except GeminiWebError as e:
                rows.append(("tải ảnh lên", False, str(e)[:120]))
        if test_prompt:
            try:
                payload = self.ask_json('Trả lời bằng đúng một khối mã ```json chứa mảng [{"ok": true, "text": "pong"}], không viết gì thêm.',
                                        (test_upload,) if test_upload else (), label="selfcheck")
                rows.append(("gửi prompt → nhận & bóc JSON", bool(payload), json.dumps(payload, ensure_ascii=False)[:80]))
                loc, n = self._response_locator()
                rows.append(("selector response_container", n > 0, f"{n} phản hồi"))
            except GeminiWebError as e:
                rows.append(("gửi prompt → nhận & bóc JSON", False, str(e)[:160]))
        self.dump_debug("check_final")
        return rows
