"""Bóc tách JSON từ câu trả lời của Gemini Web + PHÁT HIỆN response là HTML (hoặc lỗi khác) thay vì JSON.

`analyze_response()` không chỉ trả "parse lỗi": nó phân loại nguyên nhân để cảnh báo chính xác —
  • HTML_ERROR_PAGE  : trang lỗi mạng/proxy/máy chủ (502, 503, captcha, "unusual traffic"...)
  • HTML_DOM_MARKUP  : selector lấy nhầm markup thô của component Gemini → Gemini đã đổi cấu trúc trang
  • HTML_PAGE        : nguyên một trang HTML (redirect/điều hướng lạ)
  • LOGIN_REQUIRED / BLOCKED / SERVER_ERROR / NETWORK / REFUSED / EMPTY / NO_RESPONSE_ELEMENT
  • TRUNCATED_JSON   : JSON bị cắt cụt (khôi phục được một phần)
  • PROSE            : Gemini trả lời bằng văn xuôi, không có JSON
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from utils import (GeminiWebError, WebAuthError, WebBlockedError, WebDOMError, WebNetworkError,
                   WebResponseFormatError)


class ResponseKind(str, Enum):
    JSON_OK = "json_ok"
    TRUNCATED_JSON = "truncated_json"
    HTML_ERROR_PAGE = "html_error_page"
    HTML_DOM_MARKUP = "html_dom_markup"
    HTML_PAGE = "html_page"
    LOGIN_REQUIRED = "login_required"
    BLOCKED = "blocked"
    SERVER_ERROR = "server_error"
    NETWORK = "network"
    REFUSED = "refused"
    EMPTY = "empty"
    NO_RESPONSE_ELEMENT = "no_response_element"
    PROSE = "prose"


@dataclass
class Diagnosis:
    kind: ResponseKind
    message: str = ""
    hint: str = ""
    retryable: bool = False
    evidence: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind is ResponseKind.JSON_OK

    def to_exception(self) -> GeminiWebError:
        text = f"{self.message} → {self.hint}" if self.hint else self.message
        k = ResponseKind
        if self.kind is k.LOGIN_REQUIRED:
            return WebAuthError(text)
        if self.kind is k.BLOCKED:
            return WebBlockedError(text)
        if self.kind in (k.HTML_DOM_MARKUP, k.HTML_PAGE, k.NO_RESPONSE_ELEMENT):
            return WebDOMError(text)
        if self.kind in (k.HTML_ERROR_PAGE, k.NETWORK, k.SERVER_ERROR):
            return WebNetworkError(text)
        return WebResponseFormatError(text)


# ════════════════════════════════════════════════════════════
# 1. Bóc JSON
# ════════════════════════════════════════════════════════════
_FENCE_RE = re.compile(r"```[ \t]*([\w+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)
_OPEN_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n(.*)$", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([\]}])")
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‟": '"', "＂": '"'})


def _valid_payload(obj: Any) -> list[dict] | None:
    """Chấp nhận mảng các object; hoặc object bọc mảng ({"segments": [...]})."""
    if isinstance(obj, dict):
        obj = next((v for v in obj.values() if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)), None)
    if isinstance(obj, list) and obj and all(isinstance(i, dict) for i in obj):
        return obj
    return None


def _decode_from_brackets(chunk: str, max_tries: int = 40) -> list[dict] | None:
    decoder = json.JSONDecoder()
    tries = 0
    for m in re.finditer(r"[\[{]", chunk):
        tries += 1
        if tries > max_tries:
            break
        try:
            obj, _ = decoder.raw_decode(chunk, m.start())
        except json.JSONDecodeError:
            continue
        payload = _valid_payload(obj)
        if payload:
            return payload
    return None


def _salvage_truncated(chunk: str) -> list[dict] | None:
    """Mảng bị cắt giữa chừng → lấy các phần tử object đã hoàn chỉnh."""
    start = chunk.find("[")
    while start != -1:
        rest = chunk[start + 1:].lstrip()
        if rest.startswith("{"):
            break
        start = chunk.find("[", start + 1)
    if start == -1:
        return None
    decoder, idx, items, n = json.JSONDecoder(), start + 1, [], len(chunk)
    while idx < n:
        while idx < n and chunk[idx] in " \t\r\n,":
            idx += 1
        if idx >= n or chunk[idx] == "]":
            break
        try:
            obj, idx = decoder.raw_decode(chunk, idx)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            items.append(obj)
    return items or None


def extract_json_payload(text: str) -> tuple[list[dict] | None, str, bool]:
    """→ (payload, phương pháp, partial). partial=True nếu chỉ khôi phục được phần đầu của mảng bị cắt cụt."""
    if not text or not text.strip():
        return None, "empty", False
    chunks: list[tuple[str, str]] = [("fence", m.group(2)) for m in _FENCE_RE.finditer(text)]
    chunks.append(("raw", text))
    for method, chunk in chunks:
        payload = _decode_from_brackets(chunk)
        if payload:
            return payload, method, False
        cleaned = _TRAILING_COMMA_RE.sub(r"\1", chunk.translate(_SMART_QUOTES))
        if cleaned != chunk:
            payload = _decode_from_brackets(cleaned)
            if payload:
                return payload, method + "+repaired", False

    open_fence = _OPEN_FENCE_RE.search(text)   # khối ```json chưa được đóng (bị cắt)
    for chunk in ([open_fence.group(1)] if open_fence else []) + [text]:
        salvaged = _salvage_truncated(_TRAILING_COMMA_RE.sub(r"\1", chunk.translate(_SMART_QUOTES)))
        if salvaged:
            return salvaged, "salvaged", True
    return None, "none", False


# ════════════════════════════════════════════════════════════
# 2. Phát hiện HTML
# ════════════════════════════════════════════════════════════
_TAG_RE = re.compile(r"</?([a-zA-Z][\w:-]*)(?:\s[^<>]*)?/?>")
_DOM_MARKERS = ("_ngcontent", "_nghost", "ng-version", "ng-star-inserted", "jsaction=", "jsname=", "data-test-id",
                "<model-response", "<message-content", "<rich-textarea", "<code-block", "<response-container",
                "<user-query", "class=\"ql-", "mat-mdc", "cdk-")
_ERROR_PAGE_MARKERS = ("<title>error", "<title>502", "<title>503", "<title>504", "<title>404", "bad gateway",
                       "service unavailable", "gateway timeout", "unusual traffic", "/sorry/", "recaptcha",
                       "this site can’t be reached", "this site can't be reached", "err_connection", "err_name_not_resolved",
                       "err_internet_disconnected", "access denied", "request blocked", "cloudflare", "403 forbidden",
                       "404 not found", "the requested url", "we're sorry", "we’re sorry")


@dataclass
class HtmlEvidence:
    is_html: bool
    is_document: bool
    tag_count: int
    dom_markers: list[str]
    error_markers: list[str]
    top_tags: list[str]


def html_evidence(text: str) -> HtmlEvidence:
    low = text.lower()
    head = low.lstrip()[:200]
    is_doc = head.startswith(("<!doctype html", "<html", "<?xml")) or ("<html" in low[:2000] and "</html>" in low)
    tags = [m.group(1).lower() for m in _TAG_RE.finditer(text)]
    dom = [m for m in _DOM_MARKERS if m in low]
    err = [m for m in _ERROR_PAGE_MARKERS if m in low]
    wrapped = text.strip().startswith("<") and text.strip().endswith(">") and len(tags) >= 1
    is_html = is_doc or len(tags) >= 4 or (bool(dom) and len(tags) >= 1) or wrapped
    top = [t for t, _ in sorted(((t, tags.count(t)) for t in set(tags)), key=lambda x: -x[1])[:6]]
    return HtmlEvidence(is_html, is_doc, len(tags), dom, err, top)


# ════════════════════════════════════════════════════════════
# 3. Phân loại nguyên nhân
# ════════════════════════════════════════════════════════════
_LOGIN = ("sign in to", "sign in with google", "choose an account", "to continue to gemini", "đăng nhập để",
          "đăng nhập vào", "chọn một tài khoản")
_BLOCKED = ("unusual traffic", "captcha", "verify you are a human", "verify it's you", "verify it’s you",
            "too many requests", "rate limit", "reached your limit", "limit reached", "usage limit",
            "đã đạt đến giới hạn", "quá nhiều yêu cầu", "lưu lượng bất thường", "xác minh bạn là người")
_SERVER = ("something went wrong", "đã xảy ra lỗi", "có lỗi xảy ra", "an error occurred", "couldn't complete",
           "couldn’t complete", "having trouble", "temporarily unavailable", "service unavailable",
           "internal server error", "bad gateway", "gateway timeout", "please try again", "hãy thử lại")
_NETWORK = ("network error", "check your connection", "you are offline", "you're offline", "no internet",
            "err_internet_disconnected", "err_connection", "err_name_not_resolved", "err_timed_out",
            "lỗi mạng", "không có kết nối", "kiểm tra kết nối")
_REFUSAL = ("i can't help with that", "i can’t help with", "i'm not able to help", "i’m not able to help",
            "i cannot fulfill", "i can't process", "against my guidelines", "tôi không thể giúp",
            "tôi không thể thực hiện", "không thể hỗ trợ", "vi phạm chính sách")


def _hits(low: str, patterns: Sequence[str]) -> list[str]:
    return [p for p in patterns if p in low]


def snippet(text: str, n: int = 160) -> str:
    return " ".join(text.split())[:n]


def analyze_response(candidates: Sequence[str], *, page_url: str | None = None,
                     signals: dict[str, Any] | None = None) -> tuple[list[dict] | None, Diagnosis]:
    """`candidates`: các bản văn có thể chứa JSON (ưu tiên khối <code> trước, văn bản đầy đủ sau cùng).
    `signals` (tuỳ chọn): response_count, online, timed_out, generating, response_html_len."""
    signals = signals or {}
    k = ResponseKind
    partial_payload: list[dict] | None = None

    for text in candidates:
        payload, method, partial = extract_json_payload(text)
        if payload and not partial:
            return payload, Diagnosis(k.JSON_OK, f"JSON hợp lệ ({len(payload)} phần tử, cách bóc: {method})")
        if payload and partial and partial_payload is None:
            partial_payload = payload

    text = max(candidates, key=len) if candidates else ""
    low = text.lower()
    short = len(text) < 1200

    if page_url and ("accounts.google.com" in page_url or "/signin" in page_url):
        return None, Diagnosis(k.LOGIN_REQUIRED, f"Trang bị chuyển tới màn đăng nhập ({page_url[:80]}): phiên Google đã hết hạn.",
                               "Chạy `python main.py --login` để đăng nhập lại.", False, [page_url[:120]])
    if signals.get("online") is False:
        return None, Diagnosis(k.NETWORK, "Trình duyệt báo đang OFFLINE (navigator.onLine = false).",
                               "Kiểm tra kết nối Internet/proxy rồi chạy lại (kịch bản đã sinh được sẽ được giữ trong cache).", True)

    if not text.strip():
        if signals.get("response_count") == 0:
            return None, Diagnosis(k.NO_RESPONSE_ELEMENT, "Không tìm thấy phần tử câu trả lời nào trên trang sau khi gửi prompt.",
                                   "Selector `response_container` có thể đã lỗi thời (Gemini đổi cấu trúc trang) hoặc tin nhắn chưa gửi được. "
                                   "Chạy `python main.py --check-web --headed` để kiểm tra và xem web_debug/*.png.", False)
        if signals.get("timed_out"):
            return None, Diagnosis(k.NETWORK, "Hết thời gian chờ mà Gemini không trả về nội dung (mạng nghẽn hoặc phía Gemini bị treo).",
                                   "Thử lại; nếu lặp lại hãy tăng --response-timeout hoặc giảm --frames-per-prompt.", True)
        return None, Diagnosis(k.EMPTY, "Phần tử câu trả lời có nhưng rỗng.",
                               "Có thể selector `response_text` sai, hoặc phản hồi chưa render kịp; sẽ thử lại.", True,
                               [f"html_len={signals.get('response_html_len')}"])

    ev = html_evidence(text)
    if ev.is_html:
        detail = [f"{ev.tag_count} thẻ (chủ yếu: {', '.join(ev.top_tags) or '—'})"]
        if ev.error_markers:
            return None, Diagnosis(k.HTML_ERROR_PAGE,
                                   "Phản hồi là TRANG HTML LỖI thay vì JSON — dấu hiệu: " + ", ".join(ev.error_markers[:3]) + ".",
                                   "Thường do mạng nghẽn/proxy/VPN hoặc Google chặn tạm thời. Đã lưu bản chụp vào web_debug/; "
                                   "kiểm tra mạng rồi thử lại.", True, detail + ev.error_markers[:5])
        if ev.dom_markers:
            return None, Diagnosis(k.HTML_DOM_MARKUP,
                                   "Phản hồi là MARKUP HTML thô của giao diện Gemini (" + ", ".join(ev.dom_markers[:3]) +
                                   ") chứ không phải nội dung trả lời → nhiều khả năng Gemini Web đã đổi cấu trúc trang, selector đang trỏ sai phần tử.",
                                   "Chạy `python main.py --check-web --headed`, xem web_debug/*.html rồi cập nhật selector bằng "
                                   "--selectors-file (xem README).", False, detail + ev.dom_markers[:5])
        if ev.is_document:
            return None, Diagnosis(k.HTML_PAGE,
                                   "Nhận về NGUYÊN MỘT TRANG HTML thay vì câu trả lời (có thể bị điều hướng/redirect lạ hoặc trang chặn).",
                                   "Xem web_debug/*.html và *.png để biết trang nào; thử `--headed` để quan sát.", False, detail)
        return None, Diagnosis(k.HTML_DOM_MARKUP,
                               "Phản hồi chứa nhiều thẻ HTML thay vì JSON — Gemini đổi cấu trúc trang, hoặc mạng nghẽn khiến trang trả nội dung lạ.",
                               "Xem web_debug/*.html; nếu không có lỗi mạng, hãy cập nhật selector.", False, detail)

    if short:
        for kind, pats, msg, hint, retry in (
            (k.LOGIN_REQUIRED, _LOGIN, "Gemini đang yêu cầu đăng nhập.", "Chạy `python main.py --login`.", False),
            (k.BLOCKED, _BLOCKED, "Gemini chặn/giới hạn tạm thời (captcha, quá nhiều yêu cầu hoặc hết hạn mức).",
             "Chờ một lúc; nếu có captcha hãy chạy với --headed để giải tay; tăng độ trễ giữa các prompt.", False),
            (k.NETWORK, _NETWORK, "Trang báo lỗi kết nối mạng.", "Kiểm tra Internet/proxy rồi chạy lại.", True),
            (k.SERVER_ERROR, _SERVER, "Gemini báo lỗi phía máy chủ ('Something went wrong').", "Sẽ thử lại bằng cuộc trò chuyện mới.", True),
            (k.REFUSED, _REFUSAL, "Gemini từ chối xử lý yêu cầu/hình ảnh này.",
             "Thử lại; nếu lặp lại, đổi khoảng lấy mẫu hoặc chỉnh prompt.", True),
        ):
            hit = _hits(low, pats)
            if hit:
                return None, Diagnosis(kind, msg + f" (khớp: “{hit[0]}”)", hint, retry, [snippet(text)])

    if partial_payload:
        return partial_payload, Diagnosis(k.TRUNCATED_JSON,
                                          f"JSON bị cắt cụt — chỉ khôi phục được {len(partial_payload)} phần tử đầu.",
                                          "Sẽ hỏi lại; nếu vẫn cụt sẽ dùng phần khôi phục được.", True, [snippet(text[-120:])])
    if signals.get("timed_out"):
        return None, Diagnosis(k.NETWORK, "Hết thời gian chờ khi Gemini vẫn đang tạo câu trả lời (chưa có JSON hoàn chỉnh).",
                               "Tăng --response-timeout hoặc giảm --frames-per-prompt.", True, [snippet(text)])
    return None, Diagnosis(k.PROSE, f"Gemini trả lời bằng văn bản ({len(text)} ký tự) và không có khối JSON hợp lệ.",
                           "Sẽ thử lại với yêu cầu định dạng nghiêm ngặt hơn.", True, [snippet(text)])
