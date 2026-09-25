# 🎙️ Silent Video Voiceover Creator — bản Gemini Web (Playwright) + Gemini API

Video không lời → **Gemini** (giao diện web hoặc API) xem frame & viết kịch bản JSON theo timestamp → **Edge-TTS** đọc →
**pydub** dựng track audio đúng mili-giây → **FFmpeg** mux (kèm audio ducking cho nhạc nền).

## Cấu trúc

Xem **`UPGRADE_LONG_VIDEO.md`** để biết chi tiết kỹ thuật về xử lý VIDEO DÀI (phân đoạn động, checkpoint/
resume, streaming mux, trừu tượng hoá engine) — README này chỉ liệt kê vai trò từng file.

```
main.py                CLI + lệnh phụ --login / --check-web / --list-voices
long_video_pipeline.py  Orchestrator chính: chia chunk, checkpoint/resume, điều phối toàn bộ pipeline
config.py               Config dataclass (engine, TTS engine, chunking, timeout...)
checkpoint.py            State/checkpoint JSON theo 4 giai đoạn, cho TỪNG macro-chunk, resumable
chunk_planner.py         Chia video dài thành macro-chunk tại khoảng lặng/scene-cut an toàn
silence_detector.py      Phát hiện khoảng lặng bằng FFmpeg streaming (không load audio vào RAM)
time_fit.py              Công thức r = actual/target, quyết định atempo/gap-fill/clip/video-retime
video_retime.py          (opt-in) Làm chậm khung hình video khi audio vượt cả gap-fill
engines/base.py          BaseVisionEngine, BaseTTSEngine (interface OOP để hoán đổi dịch vụ)
engines/vision_gemini_web.py, vision_gemini_api.py    adapter Gemini Web/API
engines/tts_edge.py, tts_elevenlabs.py                adapter Edge-TTS/ElevenLabs
engine_factory.py        Registry: cấu hình chuỗi -> đúng class engine
gemini_web_driver.py     Playwright Persistent Context: đăng nhập tay, chọn model, upload, cào DOM
response_parser.py       Bóc JSON + phát hiện response là HTML/lỗi thay vì JSON
gemini_api_driver.py     Gemini API (google-genai)
video_processor.py       OpenCV: chia cửa sổ, trích frame (toàn video HOẶC theo từng chunk)
script_generator.py      Prompt theo lô ảnh, cache từng lô, kiểm tra số từ <-> thời lượng
tts_engine.py            Đồng bộ TTS/time-fit, dùng chung cho mọi BaseTTSEngine
audio_assembler.py       AudioSegment.silent + overlay đúng mili-giây (dùng cho từng chunk)
video_muxer.py           Ducking BGM + mux, có bản streaming theo chunk (mux_video_streaming)
utils.py                 exception, logging, timestamp, run_cmd, probe_media
tests/                   9 file test offline (unit + tích hợp quy mô nhỏ), xem UPGRADE_LONG_VIDEO.md
```

## 🖱️ Muốn dùng bằng giao diện đồ hoạ (không gõ lệnh)?

Xem **`GUI.md`** — cài đặt 1 lần như bên dưới, sau đó chỉ cần double-click `Mo_Tool.bat` mỗi lần dùng.

## Cài đặt

```bash
pip install -r requirements.txt
# FFmpeg + ffprobe phải có trong PATH. Trình duyệt: cần Google Chrome (mặc định), hoặc:
#   playwright install chrome      # để Playwright cài Chrome
#   playwright install chromium    # rồi dùng --browser chromium
```

## Đăng nhập Gemini Web (một lần)

```bash
python main.py --login
```
Tool mở một cửa sổ **Chrome bình thường** với profile riêng (`~/.silent_video_voiceover/gemini_profile`). Bạn đăng nhập tài
khoản Google, quay lại terminal nhấn Enter ("Đã đăng nhập"), rồi đóng cửa sổ. Tool kiểm tra lại phiên ở chế độ headless và báo kết quả.
Từ đó mọi lần chạy đều dùng lại profile (mặc định headless). Nếu chạy `python main.py video.mp4` khi chưa có phiên,
tool sẽ tự gợi ý đăng nhập.

> Profile chứa phiên đăng nhập của bạn — coi như mật khẩu: không chia sẻ, không đưa vào git. Không chạy hai tiến trình dùng chung một profile.

## Sử dụng

```bash
python main.py --check-web --headed                 # kiểm tra phiên + selector (nên chạy trước lần đầu)
python main.py demo.mp4                             # engine web (mặc định)
python main.py demo.mp4 --engine api                # Gemini API (cần GEMINI_API_KEY)
python main.py demo.mp4 --stop-after 2              # chỉ tạo kịch bản → sửa output/demo/script.json → chạy lại
python main.py demo.mp4 --mode scene --frames-per-prompt 6 --web-model "Flash|Fast"
python main.py demo.mp4 --script-file my_script.json    # không cần Gemini
python main.py long_demo.mp4 --chunk-minutes 5          # VIDEO DÀI (30-60+ phút): tự chia chunk, có checkpoint/resume
python main.py long_demo.mp4 --tts-engine elevenlabs --voice <voice_id>   # đổi engine TTS
```
Kết quả trong `output/<tên video>/`: `script.json`, `final_voiceover.mp3`, `output_dubbed_video.mp4`, `sync_report.json`;
`work/` chứa frame, audio từng đoạn, `pipeline.log`, `web_debug/`, và `checkpoints/` (state theo từng chunk — xem
`UPGRADE_LONG_VIDEO.md`). Mất mạng/bị chặn/Ctrl+C giữa chừng: chạy lại **đúng lệnh cũ** để tự resume từ chunk dở dang.

## Response là HTML thay vì JSON — tool báo gì?

`response_parser.analyze_response()` phân loại nguyên nhân (mỗi loại có cảnh báo + gợi ý riêng, và lưu ảnh chụp/HTML/văn bản vào `work/web_debug/`):

| Loại | Ý nghĩa | Xử lý |
|---|---|---|
| `html_error_page` | Trang lỗi 502/503, captcha, "unusual traffic", lỗi kết nối | thử lại (mạng) |
| `html_dom_markup` | Cào nhầm markup Angular thô (`_ngcontent`, `<message-content>`…) → **Gemini đổi DOM** | dừng, chỉ cách sửa selector |
| `html_page` | Nguyên một trang HTML lạ (redirect) | dừng, xem `web_debug/` |
| `login_required` | Bị chuyển tới accounts.google.com / yêu cầu đăng nhập | dừng → `--login` |
| `blocked` | Captcha, quá nhiều yêu cầu, hết hạn mức | dừng, chờ / `--headed` giải tay |
| `server_error` / `network` / `refused` / `empty` / `prose` / `truncated_json` | lỗi máy chủ, mạng, bị từ chối, rỗng, văn xuôi, JSON cụt | thử lại cuộc trò chuyện mới (có nhắc định dạng nghiêm ngặt); JSON cụt thì dùng phần khôi phục được |

## Khi Gemini đổi giao diện

Giao diện web không có API ổn định; selector có thể hỏng bất cứ lúc nào. Chạy `python main.py --check-web --headed`
để biết hạng mục nào lỗi (in bảng ✔/✖ + lưu ảnh chụp/HTML). Rồi tạo `selectors.json` chứa selector mới — chúng được thử **trước** selector mặc định:

```json
{ "prompt_box": ["div.new-editor[contenteditable]"], "send_button": ["button.new-send"] }
```
```bash
python main.py demo.mp4 --selectors-file selectors.json
```
Khoá hợp lệ: `prompt_box, send_button, stop_button, upload_menu_button, upload_files_item, file_input, file_previews,
response_container, code_blocks, error_banner, model_picker, model_options, signin_link`.

## Lưu ý

* **Điều khoản & độ ổn định:** tự động hoá giao diện web tiêu dùng của Gemini có thể trái điều khoản sử dụng của Google, dễ hỏng khi
  giao diện đổi, và tài khoản có thể bị giới hạn/khoá tạm thời. Tool nghỉ giữa các prompt (`--web-delay`) và không dùng kỹ thuật
  che giấu tự động hoá; nếu cần ổn định hãy dùng `--engine api`.
* **Headless:** nếu Google hiển thị xác minh/captcha khi headless, chạy `--headed`.
* **Gemini Web nhận tối đa ~10 ảnh/prompt** (`--frames-per-prompt` ≤ 10). Mỗi ảnh được đóng dấu timestamp ở góc trên-trái.
* **Tốc độ đọc:** mặc định 3.2 từ/giây cho tiếng Việt (đếm từng tiếng) — chỉnh bằng `--wps`. Bước TTS đo thời lượng thật.
* **"Hạ BGM 70%"** = BGM còn 70% âm lượng khi có giọng (`--bgm-duck 0.7`); muốn giọng nổi hơn dùng 0.3–0.5.
* **Model API:** Gemini 1.5/2.0 đã bị tắt, 2.5 Flash dự kiến tắt 16/10/2026 → mặc định `gemini-3.5-flash` (đổi bằng `--gemini-model`).
* Edge-TTS lỗi 403: `pip install -U edge-tts`.
