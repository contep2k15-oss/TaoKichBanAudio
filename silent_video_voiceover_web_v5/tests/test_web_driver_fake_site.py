"""Kiểm thử driver Playwright trên trang GIẢ LẬP Gemini (cần Chromium của Playwright):
    python tests/test_web_driver_fake_site.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import gemini_web_driver as gwd  # noqa: E402
from config import Config  # noqa: E402
from fake_gemini_site import FakeGeminiServer  # noqa: E402
from utils import (PipelineError, WebAuthError, WebDOMError, WebNetworkError, setup_logging)  # noqa: E402

GWD = gwd.GeminiWebDriver
GWD._backoff = lambda self, attempt: None          # type: ignore[method-assign]   # test nhanh, bỏ chờ backoff
ANGULAR = '<message-content _ngcontent-ng-c1 class="ng-star-inserted"><div class="markdown"><p>Xin chào</p></div></message-content>'


def check(name: str, cond: bool, extra: str = "") -> None:
    assert cond, f"{name} {extra}"
    print("  ✓", name)


def make_cfg(tmp: Path, srv: FakeGeminiServer, query: str = "", **kw) -> Config:
    return Config(output_dir=tmp / "out", user_data_dir=tmp / "profile", browser_channel="chromium", headless=True,
                  gemini_url=srv.url(query), web_stable_sec=0.8, web_response_timeout_sec=30, web_upload_timeout_sec=20,
                  web_delay_between_prompts_sec=0, web_retries=2, **kw)


def images(tmp: Path, n: int) -> list[Path]:
    out = []
    for i in range(n):
        p = tmp / f"f{i}.jpg"
        cv2.imwrite(str(p), np.full((90, 160, 3), 40 * (i + 1), np.uint8))
        out.append(p)
    return out


def main() -> None:
    srv = FakeGeminiServer()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cfg = make_cfg(tmp, srv)
        cfg.ensure_dirs()
        setup_logging(None, verbose=False)
        imgs = images(tmp, 3)

        # 1) đăng nhập / chưa đăng nhập
        with GWD(cfg) as drv:
            check("phát hiện ĐÃ đăng nhập", drv.is_logged_in())
        with GWD(make_cfg(tmp, srv, "?logged_out=1")) as drv:
            check("phát hiện CHƯA đăng nhập (nút Sign in)", not drv.is_logged_in())
            try:
                drv.ensure_logged_in()
                raise AssertionError("phải ném WebAuthError")
            except WebAuthError:
                print("  ✓ ensure_logged_in ném WebAuthError")

        with GWD(cfg) as drv:
            # 2) luồng đầy đủ: chọn model + upload qua file chooser + gửi + đợi streaming + bóc JSON
            srv.requests.clear()
            payload = drv.ask_json("Viết kịch bản.", imgs, label="t2")
            req = srv.requests[-1]
            check("bóc được JSON từ code-block", payload[0]["text"] == "Xin chào các bạn" and payload[0]["tone"] == "vui ve")
            check("đã upload đủ 3 ảnh qua file chooser", req["n_files"] == 3, str(req))
            check("đã chọn model 'Fast' trong menu", req["model"] == "Fast", req["model"])
            check("prompt được điền nguyên vẹn", req["prompt"].strip() == "Viết kịch bản.")

            # 3) văn xuôi rồi mới JSON → thử lại với yêu cầu nghiêm ngặt
            srv.requests.clear()
            srv.queue.append({"render": "text", "text": "Mình thấy các ảnh mô tả một buổi nấu ăn rất vui nhộn, bạn muốn mình viết gì tiếp theo nhỉ?"})
            payload = drv.ask_json("Viết kịch bản.", (), label="t3")
            check("PROSE → retry → JSON", len(srv.requests) == 2 and "LƯU Ý QUAN TRỌNG" in srv.requests[1]["prompt"] and payload)

            # 4) response là HTML trang lỗi (mạng nghẽn) → retry hết → WebNetworkError
            html_err = "<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head><body><h1>Bad Gateway</h1></body></html>"
            srv.queue.extend([{"render": "text", "text": html_err}] * 2)
            try:
                drv.ask_json("x", (), label="t4")
                raise AssertionError("phải ném WebNetworkError")
            except WebNetworkError as e:
                check("HTML trang lỗi → WebNetworkError có cảnh báo chính xác", "TRANG HTML LỖI" in str(e), str(e))

            # 5) response là markup Angular thô (DOM đổi) → WebDOMError NGAY, không retry, có file debug
            srv.requests.clear()
            srv.queue.append({"render": "text", "text": ANGULAR})
            try:
                drv.ask_json("x", (), label="t5")
                raise AssertionError("phải ném WebDOMError")
            except WebDOMError as e:
                check("markup Angular → WebDOMError, không retry", len(srv.requests) == 1 and "MARKUP HTML thô" in str(e), str(e))
            dumps = sorted(p.suffix for p in cfg.web_debug_dir.glob("*_t5_a1_html_dom_markup.*"))
            check("đã lưu screenshot/html/txt chẩn đoán", dumps == [".html", ".png", ".txt"], str(dumps))

            # 6) JSON bị cắt cụt cả 2 lần → dùng phần khôi phục
            cut = '```json\n[{"id": 1, "start_time": "00:00:00.5", "end_time": "00:00:02", "text": "một"}, {"id": 2, "start_time": "00:00:02.5", "te'
            srv.queue.extend([{"render": "text", "text": cut}] * 2)
            payload = drv.ask_json("x", (), label="t6")
            check("JSON cắt cụt → khôi phục phần đầu", len(payload) == 1 and payload[0]["text"] == "một")

            # 7) phiên hết hạn giữa chừng (redirect sang accounts.google.com)
            drv.ctx.route("https://accounts.google.com/**", lambda r: r.fulfill(body="<html><body>Sign in</body></html>", content_type="text/html"))
            srv.queue.append({"redirect": "https://accounts.google.com/v3/signin/identifier"})
            try:
                drv.ask_json("x", (), label="t7")
                raise AssertionError("phải ném WebAuthError")
            except WebAuthError as e:
                check("redirect đăng nhập → WebAuthError", "--login" in str(e))
            drv.ctx.unroute("https://accounts.google.com/**")

            # 8) selfcheck
            rows = drv.selfcheck(test_upload=imgs[0])
            bad = [r for r in rows if not r[1]]
            check("selfcheck: mọi hạng mục đều đạt", not bad, str(bad))

            # 9) mất mạng → WebNetworkError
            drv.ctx.set_offline(True)
            try:
                drv.ask_json("x", (), label="t9")
                raise AssertionError("phải ném WebNetworkError")
            except WebNetworkError:
                print("  ✓ mất mạng → WebNetworkError")
            drv.ctx.set_offline(False)

        # 10) giao diện đổi (editor2): mặc định lỗi DOM rõ ràng; --selectors-file khắc phục
        v2 = make_cfg(tmp, srv, "?v2=1")
        with GWD(v2) as drv:
            try:
                drv.ask_json("x", (), label="t10")
                raise AssertionError("phải ném WebDOMError")
            except WebDOMError as e:
                check("DOM đổi → WebDOMError chỉ đường sửa", "prompt_box" in str(e))
        sf = tmp / "sel.json"
        sf.write_text(json.dumps({"prompt_box": "div.editor2"}), encoding="utf-8")
        v2b = make_cfg(tmp, srv, "?v2=1", selectors_file=sf)
        with GWD(v2b) as drv:
            check("selectors-file khắc phục được", bool(drv.ask_json("x", (), label="t10b")))
        try:
            bad_sf = tmp / "bad.json"
            bad_sf.write_text('{"khong_ton_tai": ["a"]}', encoding="utf-8")
            gwd.Selectors.load(bad_sf)
            raise AssertionError("phải báo khoá lạ")
        except PipelineError:
            print("  ✓ selectors-file khoá lạ bị từ chối")
    srv.close()
    print("TẤT CẢ PASS ✔")


if __name__ == "__main__":
    main()
