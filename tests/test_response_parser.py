"""Unit test OFFLINE cho response_parser (không cần trình duyệt/mạng):  python tests/test_response_parser.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from response_parser import ResponseKind as K, analyze_response  # noqa: E402
from utils import WebAuthError, WebBlockedError, WebDOMError, WebNetworkError, WebResponseFormatError  # noqa: E402

GOOD = '[{"id": 1, "start_time": "00:00:01.000", "end_time": "00:00:05.500", "duration_sec": 4.5, "text": "Xin chào", "tone": "hao hung"}]'


def kind(texts, **kw):
    payload, diag = analyze_response(texts if isinstance(texts, list) else [texts], **kw)
    return payload, diag


def check(name, cond):
    assert cond, name
    print("  ✓", name)


# ── JSON hợp lệ ──
p, d = kind(f"Đây là kịch bản:\n```json\n{GOOD}\n```\nChúc bạn thành công!")
check("JSON trong khối ```json```", d.kind is K.JSON_OK and p[0]["text"] == "Xin chào")
p, d = kind(f"JSON\nCopy code\n{GOOD}")
check("JSON kèm nhãn 'JSON / Copy code' của code-block", d.kind is K.JSON_OK)
p, d = kind(f"[Ảnh 1] mô tả...\n{GOOD}")
check("có '[' trong văn xuôi trước JSON", d.kind is K.JSON_OK)
p, d = kind('```json\n[{"id": 1, "text": "a",},]\n```')
check("dấu phẩy thừa", d.kind is K.JSON_OK)
p, d = kind('[{“id”: 1, “text”: “a”}]')
check("ngoặc kép cong", d.kind is K.JSON_OK)
p, d = kind(['{"nhầm": 1}', GOOD])
check("ưu tiên ứng viên có JSON hợp lệ", d.kind is K.JSON_OK)
p, d = kind('{"segments": [{"id": 1, "text": "a"}]}')
check("object bọc mảng", d.kind is K.JSON_OK and p[0]["id"] == 1)

# ── JSON bị cắt ──
p, d = kind('```json\n[{"id": 1, "text": "a"}, {"id": 2, "text": "b"}, {"id": 3, "te')
check("JSON cắt cụt → khôi phục 2 phần tử", d.kind is K.TRUNCATED_JSON and len(p) == 2 and d.retryable)

# ── HTML ──
p, d = kind("<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head><body><h1>Bad Gateway</h1></body></html>")
check("HTML trang lỗi 502", d.kind is K.HTML_ERROR_PAGE and d.retryable and isinstance(d.to_exception(), WebNetworkError))
p, d = kind('<message-content _ngcontent-ng-c123 class="ng-star-inserted"><div class="markdown"><p>Xin chào</p></div></message-content>')
check("markup Angular thô → DOM đổi", d.kind is K.HTML_DOM_MARKUP and not d.retryable and isinstance(d.to_exception(), WebDOMError))
p, d = kind("<html><body><div>Welcome</div><div>x</div></body></html>")
check("nguyên trang HTML không dấu hiệu lỗi", d.kind is K.HTML_PAGE and isinstance(d.to_exception(), WebDOMError))
p, d = kind("<div><p>a</p><span>b</span><b>c</b><i>d</i></div>")
check("nhiều thẻ HTML chung chung", d.kind is K.HTML_DOM_MARKUP)
p, d = kind("<html><head><title>Sorry...</title></head><body>Our systems have detected unusual traffic from your computer network</body></html>")
check("HTML captcha/unusual traffic", d.kind is K.HTML_ERROR_PAGE)

# ── Nguyên nhân khác ──
p, d = kind("Something went wrong. Please try again.")
check("Something went wrong", d.kind is K.SERVER_ERROR and d.retryable)
p, d = kind("Bạn đã đạt đến giới hạn sử dụng. Hãy quay lại sau.")
check("hết hạn mức", d.kind is K.BLOCKED and isinstance(d.to_exception(), WebBlockedError))
p, d = kind("Sign in to continue to Gemini")
check("yêu cầu đăng nhập (văn bản)", d.kind is K.LOGIN_REQUIRED and isinstance(d.to_exception(), WebAuthError))
p, d = kind("", page_url="https://accounts.google.com/v3/signin/identifier?continue=...")
check("redirect sang accounts.google.com", d.kind is K.LOGIN_REQUIRED)
p, d = kind("I can't help with that.")
check("từ chối", d.kind is K.REFUSED)
p, d = kind("Xin lỗi, tôi thấy các bức ảnh này mô tả một cảnh làm bánh rất đẹp mắt và hấp dẫn.")
check("văn xuôi không JSON", d.kind is K.PROSE and isinstance(d.to_exception(), WebResponseFormatError))
p, d = kind("", signals={"response_count": 0})
check("không thấy phần tử trả lời", d.kind is K.NO_RESPONSE_ELEMENT)
p, d = kind("", signals={"timed_out": True})
check("timeout không nội dung", d.kind is K.NETWORK)
p, d = kind("", signals={"response_count": 1, "response_html_len": 900})
check("phần tử rỗng", d.kind is K.EMPTY)
p, d = kind("bất kỳ", signals={"online": False})
check("navigator.onLine = false", d.kind is K.NETWORK)

# Văn xuôi dài nhắc tới 'captcha' không được coi là bị chặn
long_text = "Trong video này " * 150 + "captcha"
p, d = kind(long_text)
check("văn xuôi dài chứa từ khoá không bị nhận nhầm", d.kind is K.PROSE)
print("TẤT CẢ PASS ✔")
