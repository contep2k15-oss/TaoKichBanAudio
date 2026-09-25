"""Trang giả lập giao diện Gemini Web (DOM tương tự) để kiểm thử driver Playwright OFFLINE.
Không phải Gemini thật: chỉ kiểm chứng logic điều khiển (chọn model, upload qua file chooser, điền prompt, gửi,
đợi streaming, cào DOM, bóc JSON, phân loại lỗi)."""
from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Fake Gemini</title></head><body>
<header>__AUTH__</header><main id="chat"></main>
<div id="previews"></div>
<div class="input-area">
  <button class="input-area-switch" data-test-id="bard-mode-menu-button" id="picker">Pro</button>
  <div id="mode-menu" hidden>
    <button role="menuitemradio">Fast</button><button role="menuitemradio">Thinking</button><button role="menuitemradio">Pro</button></div>
  <button aria-label="Open upload file menu" id="plus">+</button>
  <div id="upmenu" hidden role="menu"><button role="menuitem" data-test-id="local-images-files-uploader-button" id="upitem">Upload files</button></div>
  <rich-textarea><div __EDITOR__ contenteditable="true"></div></rich-textarea>
  <button class="send-button" aria-label="Send message" disabled>Send</button>
  <button class="stop" aria-label="Stop response" hidden>Stop</button>
</div>
<script>
const $ = s => document.querySelector(s);
const editor = $('[contenteditable]'), send = $('.send-button'), stop = $('.stop');
let files = 0, uploading = false;
const refresh = () => { send.disabled = uploading || !editor.innerText.trim(); };
editor.addEventListener('input', refresh);
editor.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !send.disabled) { e.preventDefault(); go(); } });
$('#picker').onclick = () => { const m = $('#mode-menu'); m.hidden = !m.hidden; };
document.querySelectorAll('#mode-menu button').forEach(b => b.onclick = () => { $('#picker').textContent = b.textContent; $('#mode-menu').hidden = true; });
$('#plus').onclick = () => { const m = $('#upmenu'); m.hidden = !m.hidden; };
$('#upitem').onclick = () => {
  $('#upmenu').hidden = true;
  const inp = document.createElement('input'); inp.type = 'file'; inp.multiple = true;
  inp.onchange = () => {
    uploading = true; refresh();
    [...inp.files].forEach(f => { const p = document.createElement('uploader-file-preview'); p.textContent = f.name; $('#previews').appendChild(p); files++; });
    setTimeout(() => { uploading = false; refresh(); }, 600);
  };
  inp.click();
};
send.onclick = go;
async function go() {
  const prompt = editor.innerText; editor.innerText = ''; refresh();
  const resp = document.createElement('model-response'); resp.className = 'response-container';
  resp.innerHTML = '<message-content><div class="markdown"></div></message-content>';
  $('#chat').appendChild(resp); const md = resp.querySelector('.markdown');
  stop.hidden = false;
  const r = await (await fetch('/api/generate', {method: 'POST', body: JSON.stringify({prompt, n_files: files, model: $('#picker').textContent})})).json();
  if (r.redirect) { location.href = r.redirect; return; }
  let i = 0;
  const tick = () => {
    i = Math.min(r.text.length, i + 12); const part = r.text.slice(0, i);
    if (r.render === 'json') {
      const m = part.match(/^([\\s\\S]*?)```json\\n([\\s\\S]*)$/);
      if (m) { const body = m[2].replace(/```[\\s\\S]*$/, '');
        md.innerHTML = ''; const p = document.createElement('p'); p.textContent = m[1]; md.appendChild(p);
        const cb = document.createElement('code-block'); cb.innerHTML = '<div class="code-block-decoration">JSON <button>Copy code</button></div><pre><code></code></pre>';
        cb.querySelector('code').textContent = body; md.appendChild(cb);
      } else { md.textContent = part; }
    } else if (r.render === 'markup') { md.innerHTML = r.text; }
    else { md.textContent = part; }          // 'text': textContent → inner_text giữ nguyên chuỗi (kể cả khi nó là HTML)
    if (i < r.text.length) setTimeout(tick, 30); else stop.hidden = true;
  };
  setTimeout(tick, 400);
}
</script></body></html>"""


class FakeGeminiServer:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.queue: deque[dict] = deque()
        self.reply_fn = None            # tuỳ chọn: callable(request_dict) -> chuỗi trả lời
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # im lặng
                pass

            def do_GET(self):
                logged_out = "logged_out=1" in self.path
                auth = ('<a href="https://accounts.google.com/ServiceLogin?continue=x">Sign in</a>' if logged_out
                        else '<a aria-label="Google Account: test">T</a>')
                html = PAGE.replace("__AUTH__", auth).replace("__EDITOR__", 'class="editor2"' if "v2=1" in self.path else 'class="ql-editor" role="textbox" aria-label="Enter a prompt here"')
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                outer.requests.append(data)
                resp = outer.queue.popleft() if outer.queue else {
                    "render": "json", "text": outer.reply_fn(data) if outer.reply_fn else outer.default_reply()}
                body = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @staticmethod
    def default_reply() -> str:
        seg = [{"id": 1, "start_time": "00:00:00.500", "end_time": "00:00:03.000", "duration_sec": 2.5,
                "text": "Xin chào các bạn", "tone": "vui ve"}]
        return "Đây là kịch bản bạn cần:\n```json\n" + json.dumps(seg, ensure_ascii=False, indent=2) + "\n```\nChúc bạn thành công!"

    def url(self, query: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/app{query}"

    def close(self) -> None:
        self.httpd.shutdown()
