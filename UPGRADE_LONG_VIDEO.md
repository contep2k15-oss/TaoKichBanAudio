# Nâng cấp: Xử lý VIDEO DÀI (30–60+ phút)

Tài liệu này giải thích 4 nâng cấp kiến trúc đã thêm vào dự án, lấy cảm hứng từ pyvideotrans (chia nhỏ
theo audio, kiến trúc "silent track + overlay") và Auto_Video_Dubbing (pipeline theo giai đoạn có
checkpoint). Xem `README.md` gốc cho hướng dẫn cài đặt/sử dụng cơ bản; tài liệu này chỉ nói về phần MỚI.

## Vì sao cần nâng cấp?

Bản gốc xử lý cả video trong MỘT lượt: trích hết frame, gửi hết batch cho Gemini, tổng hợp hết TTS, rồi
dựng MỘT track audio dài bằng đúng video. Với video 5–10 phút, cách này ổn. Với video 30–60+ phút:

- **RAM**: `AudioSegment.silent(duration=total_ms)` cho 60 phút stereo 44.1kHz float32 (lúc ducking) tốn
  hơn 1 GB; nếu máy yếu hoặc chạy nhiều video song song, dễ tràn RAM.
- **Token limit / giới hạn upload**: gửi hàng trăm frame cùng lúc cho Gemini vượt giới hạn ảnh/prompt.
- **Rủi ro mất công**: video dài → thời gian chạy dài (hàng chục phút tới vài giờ) → xác suất rớt mạng/lỗi
  giữa chừng cao hơn nhiều; không có cách nào tiếp tục ngoài chạy lại từ đầu.

## 1. Phân đoạn động (`chunk_planner.py`, `silence_detector.py`)

Video được **tự động chia thành các "macro-chunk"** (mặc định ~4 phút/chunk, chỉnh bằng
`--chunk-minutes`) TRƯỚC KHI xử lý bất cứ gì khác. Mỗi chunk sau đó đi qua toàn bộ pipeline (trích frame →
kịch bản → TTS → ghép) **độc lập** với các chunk khác.

Điểm cắt KHÔNG cố định theo thời gian — thứ tự ưu tiên:
1. **Khoảng lặng** gần mốc mục tiêu nhất (quét bằng `ffmpeg -af silencedetect`, streaming — xem mục 3).
2. **Điểm chuyển cảnh** (scene cut, tái dùng `video_processor.detect_scene_cuts`, OpenCV histogram HSV).
3. Cắt cứng tại đúng mốc mục tiêu (có cảnh báo trong log) — chỉ khi không có gì trong dung sai
   (`--chunk-tolerance-sec`, mặc định 45s), ví dụ giọng nói liên tục không ngớt suốt video.

```bash
python main.py long_demo.mp4 --chunk-minutes 5 --chunk-tolerance-sec 60
```

Video ngắn hơn `--chunk-minutes` tự động chỉ có **1 chunk bao trọn video** — không có nhánh code riêng cho
"video ngắn" và "video dài", cùng một pipeline xử lý cả hai (`long_video_pipeline.py`).

## 2. Time-Stretch qua `atempo` (`time_fit.py`)

`time_fit.compute_fit()` là một hàm thuần, tách khỏi mọi I/O, thực hiện đúng công thức đề bài:

```python
r = actual_duration / target_duration
```

- `r ≤ 1`: giữ nguyên tốc độ (không bao giờ làm CHẬM giọng đọc).
- `1 < r ≤ 1.25`: tăng tốc bằng FFmpeg `atempo=r` (WSOLA — giữ nguyên cao độ, khác `pydub.speedup` hay bị rè).
- `r > 1.25`: tăng tốc tối đa 1.25×, rồi **cho phép lấn khoảng trống** trước đoạn kế tiếp (gap-fill).
- Vẫn không đủ chỗ: theo `--allow-video-retime`:
  - **Tắt (mặc định)**: cắt bớt cuối audio + fade-out, pipeline vẫn chạy tiếp tự động.
  - **Bật**: đánh dấu `needs_video_retime` để `video_retime.py` LÀM CHẬM khung hình video trong đúng cửa sổ
    đó (dựng lại bằng `trim` + `setpts` + `concat` — một input, một filter_complex, không phải nhiều `-i`).

## 3. Quản lý bộ nhớ & Checkpoint (`silence_detector.py`, `checkpoint.py`, `long_video_pipeline.py`)

**Không load toàn bộ audio/video vào RAM:**
- Phát hiện khoảng lặng chạy FFmpeg với `-f null -`, đọc **từng dòng** log ở stderr — RAM hằng số, không
  phụ thuộc độ dài video (`silence_detector.py`).
- Trích frame ghi thẳng ra đĩa từng ảnh, giải phóng mảng pixel ngay sau khi ghi (`video_processor.py`).
- Ducking + mix BGM giờ làm **theo từng chunk** (`video_muxer.mux_video_streaming`): trích BGM của riêng
  chunk đó bằng FFmpeg, mix trong RAM (chỉ vài chục MB), xuất ra đĩa, rồi **nối các đoạn WAV đã mix bằng
  FFmpeg concat demuxer** (`-f concat -c copy`) — đúng kỹ thuật VideoLingo/pyvideotrans dùng để né
  `-filter_complex` với nhiều input (nguyên nhân WinError 206 trên Windows khi câu lệnh quá dài).
- Sau mỗi chunk, gọi `gc.collect()` chủ động thay vì đợi Python tự dọn.

**Checkpoint JSON theo 4 giai đoạn, cho TỪNG chunk:**

```
work/checkpoints/<fingerprint-hash>/
  00_analysis.json          # kết quả quét khoảng lặng + scene-cut (quét 1 lần, dùng lại mãi)
  00_chunks.json            # kế hoạch chia chunk
  01_extracted_chunk0000.json
  02_script_chunk0000.json
  03_tts_done_chunk0000.json
  04_assembled_chunk0000.json
  01_extracted_chunk0001.json  ...
```

`<fingerprint-hash>` là hash của video + mọi tham số cấu hình ảnh hưởng kết quả — đổi cấu hình/video
KHÔNG xoá checkpoint cũ (chỉ đơn giản là dùng thư mục khác), nên quay lại cấu hình trước vẫn resume được.

**Resume tự động:** chạy lại **đúng lệnh cũ**, pipeline tự nhận diện chunk nào đã xong (đủ cả 4 giai đoạn)
để bỏ qua, và tiếp tục đúng từ giai đoạn dở dang của chunk đang xử lý — không cần cờ đặc biệt:

```bash
python main.py long_demo.mp4 --chunk-minutes 4     # rớt mạng ở phút 25...
python main.py long_demo.mp4 --chunk-minutes 4     # chạy lại y hệt lệnh trên → tự resume
```

Muốn làm lại một chunk cụ thể (vd kết quả kịch bản của nó không ổn): `--force-chunk 5`. Muốn bỏ hết
checkpoint, chạy lại từ đầu: `--force`.

## 4. Trừu tượng hoá Engine (`engines/`, `engine_factory.py`)

```
engines/base.py              BaseVisionEngine (ask_json), BaseTTSEngine (synthesize), Prosody
engines/vision_gemini_web.py  adapter bọc GeminiWebDriver (Playwright)
engines/vision_gemini_api.py  adapter bọc GeminiApiDriver (google-genai)
engines/tts_edge.py           adapter Edge-TTS (miễn phí)
engines/tts_elevenlabs.py     adapter ElevenLabs (trả phí, giọng tự nhiên hơn) — xem lưu ý bên dưới
engine_factory.py             registry: chuỗi cấu hình ('web'/'api', 'edge'/'elevenlabs') → đúng class
```

Toàn bộ phần còn lại của pipeline (`chunk_planner`, `script_generator`, `tts_engine`, `audio_assembler`,
`video_muxer`) chỉ gọi qua hai interface này — KHÔNG biết và không cần biết đang dùng Gemini Web hay API,
Edge-TTS hay ElevenLabs. Đổi dịch vụ chỉ là đổi cờ CLI, không đụng gì tới FFmpeg/pydub bên dưới:

```bash
python main.py demo.mp4 --engine api --tts-engine elevenlabs --voice 21m00Tcm4TlvDq8ikWAM
```

Thêm dịch vụ mới (vd Claude Vision, Azure TTS...): viết class kế thừa `BaseVisionEngine`/`BaseTTSEngine`
rồi đăng ký một dòng vào `VISION_REGISTRY`/`TTS_REGISTRY` trong `engine_factory.py` — không sửa nơi khác.

**Lưu ý về `engines/tts_elevenlabs.py`:** được viết dựa trên tài liệu API hiện hành của ElevenLabs
(endpoint `POST /v1/text-to-speech/{voice_id}`, header `xi-api-key`, `voice_settings.speed` 0.7–1.2) và đã
kiểm thử OFFLINE bằng `httpx.MockTransport` (`tests/test_elevenlabs_engine.py`) để xác nhận luồng
request/retry/lỗi đúng như thiết kế. **Chưa được gọi thử với API ElevenLabs thật** (môi trường phát triển
không có quyền truy cập mạng ra `api.elevenlabs.io`) — trước khi dùng thật, hãy thử với 1 câu ngắn
(`--tts-engine elevenlabs --stop-after 3`) và đối chiếu lại tài liệu mới nhất tại
https://elevenlabs.io/docs/api-reference/text-to-speech, vì các dịch vụ bên thứ ba có thể đổi API theo thời gian.

## Cờ CLI mới (tóm tắt)

| Cờ | Mặc định | Ý nghĩa |
|---|---|---|
| `--chunk-minutes` | 4.0 | độ dài mục tiêu mỗi macro-chunk |
| `--chunk-tolerance-sec` | 45.0 | dung sai tìm điểm cắt an toàn quanh mốc mục tiêu |
| `--min-chunk-sec` | 60.0 | không chunk nào ngắn hơn ngần này (trừ chunk cuối) |
| `--silence-min-sec` / `--silence-noise-db` | 0.6 / -30.0 | tham số phát hiện khoảng lặng |
| `--force-chunk INDEX` | — | chỉ làm lại một chunk cụ thể (bỏ checkpoint của riêng nó) |
| `--allow-video-retime` | tắt | last-resort: cho phép làm chậm khung hình khi audio quá dài |
| `--tts-engine` | edge | `edge` \| `elevenlabs` |
| `--elevenlabs-api-key` / `--elevenlabs-model` | — | cấu hình ElevenLabs (khi `--tts-engine elevenlabs`) |
| `--stop-after` | — | `0` (chỉ xem kế hoạch chunk) \| `2` (dừng sau kịch bản) \| `3` (dừng sau TTS) |

## Đã kiểm thử thế nào

Vì sandbox phát triển không có video dài thật (30–60 phút) và không truy cập được Gemini/ElevenLabs thật,
việc kiểm thử chia làm hai lớp:

1. **Unit test thuần, không cần video/mạng**: `test_time_fit.py` (công thức r, atempo, gap-fill, clip,
   retime), `test_chunk_planner.py` (bao gồm mô phỏng video 60 phút với khoảng lặng mỗi 4 phút → chia
   đúng 15 chunk, liên tục, không chồng lấn), `test_checkpoint.py` (mô phỏng "rớt mạng giữa chừng" bằng
   cách mở lại `PipelineCheckpoint` ở một tiến trình Python mới trỏ cùng thư mục), `test_video_retime.py`
   (dùng FFmpeg thật, xác nhận video được làm chậm đúng hệ số), `test_elevenlabs_engine.py` (mock HTTP).
2. **Tích hợp end-to-end quy mô nhỏ**: `smoke_test.py` dùng video tổng hợp NGẮN (24 giây) nhưng đặt
   `chunk_target_sec` rất nhỏ (8 giây) để ép pipeline chia thành nhiều chunk — cơ chế chia chunk không phụ
   thuộc độ dài tuyệt đối, chỉ phụ thuộc tỉ lệ target/tolerance, nên bài test này xác nhận đúng cơ chế
   (nhiều chunk, resume giữa chừng bằng cách xoá checkpoint một phần rồi chạy lại, `--stop-after`,
   `--script-file` chia đúng theo chunk) mà không cần xử lý một video dài thật hàng chục phút.

Tất cả 8 file test đều chạy được offline (`python tests/<tên file>.py`), dùng Playwright Chromium thật +
FFmpeg/OpenCV/pydub thật, chỉ giả lập (mock) hai thứ không thể gọi trong sandbox: trang Gemini Web và
Edge-TTS/ElevenLabs qua mạng.
