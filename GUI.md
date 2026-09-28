# Dùng qua giao diện đồ hoạ (GUI) — không cần gõ lệnh

## Lần đầu tiên (chỉ làm 1 lần)

Vẫn cần cài đặt qua Git Bash như hướng dẫn trong `README.md` (Python, venv, `pip install -r requirements.txt`,
FFmpeg, `python main.py --login` để đăng nhập Gemini Web). Sau khi những bước đó đã xong, **không cần
đụng tới dòng lệnh nữa** — từ giờ chỉ dùng file `Mo_Tool.bat`.

## Từ lần thứ hai trở đi: chỉ cần double-click

1. Mở thư mục dự án bằng File Explorer.
2. Double-click **`Mo_Tool.bat`**.
3. Một cửa sổ đen (terminal) hiện lên rồi tự động mở **trình duyệt** với giao diện của tool
   (`http://localhost:8501`). Đừng đóng cửa sổ đen đó khi đang dùng tool — nó là "máy chủ" chạy phía sau.
4. Thao tác hoàn toàn bằng chuột trên giao diện web vừa mở:
   - **① Đăng nhập Gemini Web**: nếu phiên đăng nhập hết hạn, bấm "Mở Chrome để đăng nhập" → đăng nhập →
     đóng cửa sổ Chrome → bấm "Tôi đã đăng nhập xong, kiểm tra lại".
   - **② Chọn video**: bấm "Duyệt file..." để mở hộp thoại chọn file như bình thường, hoặc dán đường dẫn.
   - **③ Chạy**: bấm "Tạo video thuyết minh" (chạy toàn bộ) hoặc "Chỉ tạo kịch bản" (dừng lại để xem/sửa
     kịch bản trước — giống `--stop-after 2` trên dòng lệnh).
   - **④ Kết quả**: xem trước video ngay trên trang, tải video/audio/kịch bản về máy.
5. Các tuỳ chọn (engine, giọng đọc, độ dài chunk cho video dài, tỉ lệ âm lượng nhạc nền...) nằm ở **thanh
   bên trái**, tương ứng với các cờ `--engine`, `--tts-engine`, `--chunk-minutes`, `--bgm-duck`... trên CLI.

## Muốn tạo icon riêng trên Desktop

Chuột phải `Mo_Tool.bat` → **Send to → Desktop (create shortcut)**. Shortcut hiện ra ở Desktop; muốn đổi
icon: chuột phải shortcut đó → Properties → Change Icon... → chọn icon tuỳ ý.

## Video dài, chạy lâu — có cần giữ máy tính mở suốt không?

Có — cửa sổ đen (terminal) và trình duyệt đều phải giữ mở trong lúc xử lý. Nếu tắt máy/mất mạng giữa
chừng, cứ mở lại `Mo_Tool.bat`, chọn lại đúng video đó và bấm chạy lại — tool tự nhận diện phần đã xử lý
xong (checkpoint) và tiếp tục từ chỗ dở dang, không phải làm lại từ đầu.

## Muốn quay lại dùng dòng lệnh (CLI)?

Vẫn dùng bình thường qua `python main.py ...` như trước — GUI và CLI dùng chung một pipeline, kết quả và
checkpoint tương thích với nhau (chạy dở bằng GUI, tiếp tục bằng CLI hoặc ngược lại đều được).

---

## Dùng như app desktop thật đóng gói `.exe` (khuyến nghị nhất — không cần cài Python)

Cách trên (`Mo_Tool.bat`) vẫn cần bạn cài Python + venv một lần. Cách dưới đây đóng gói TOÀN BỘ (Python,
Streamlit, Playwright, FFmpeg...) vào 1 thư mục `.exe` chạy thẳng — người nhận không cần cài gì cả, kể cả
Python. Ứng dụng cũng mở ra một **cửa sổ app riêng thật sự** (dùng `pywebview`), không phải tab trình duyệt.

### Cách A — Tải bản đã build sẵn (nhanh nhất)

1. Vào tab **Releases** của repo trên GitHub (sau khi bạn đã đẩy code lên, xem `README.md`).
2. Tải file `SilentVideoVoiceoverCreator-vX.X.X-win64.zip`, giải nén ra một thư mục bất kỳ.
3. Đổi tên `.env.example` (nằm trong thư mục vừa giải nén) thành `.env`, mở lên điền `GEMINI_API_KEY` nếu
   dùng `--engine api` (bỏ qua nếu chỉ dùng Gemini Web).
4. Chạy `SilentVideoVoiceoverCreator.exe` — một cửa sổ app riêng hiện ra, FFmpeg và Chromium đã có sẵn bên
   trong, không cần cài thêm gì. Lần đầu đăng nhập Gemini Web vẫn cần làm 1 lần như bình thường (nút "Mở
   Chrome để đăng nhập" trên giao diện).

> Windows SmartScreen có thể cảnh báo "Unknown publisher" khi chạy lần đầu (vì file `.exe` tự build chưa
> mua chứng chỉ ký số) — đây là cảnh báo bình thường, không phải virus. Bấm "More info" → "Run anyway".

### Cách B — Tự đóng gói `.exe` trên máy bạn

```bash
pip install -r requirements.txt
python gen_icon.py
pyinstaller silent_video_voiceover_tool.spec
```

Kết quả nằm ở `dist/SilentVideoVoiceoverCreator/`. Muốn có sẵn FFmpeg/Chromium bên trong (để người nhận
không cần cài gì), tải chúng vào 2 thư mục `ffmpeg_bin/` và `playwright_browsers/` (đặt cạnh file
`.spec`) TRƯỚC KHI chạy `pyinstaller` — xem đúng các bước tải này trong
`.github/workflows/build.yml` (phần "Tải Chromium..."/"Tải FFmpeg..."), copy nguyên văn chạy tay được.

### Phát hành bản mới (build tự động qua GitHub Actions, không cần máy Windows)

Mỗi khi sửa code xong muốn ra bản `.exe` mới, chỉ cần đẩy một **tag phiên bản** lên GitHub — máy ảo Windows
của GitHub Actions tự làm hết: cài Python, tải FFmpeg + Chromium, đóng gói `.exe`, và tự đăng lên tab
**Releases**:

```bash
git add -A
git commit -m "Mô tả thay đổi"
git push

git tag v1.0.0
git push origin v1.0.0
```

Theo dõi tiến trình ở tab **Actions** trên GitHub (mất khoảng 10-15 phút, chủ yếu do tải Chromium ~300MB).
Xong, file `.zip` tự xuất hiện ở tab **Releases** — gửi link đó cho bất kỳ ai muốn dùng, họ chỉ cần tải,
giải nén, điền `.env`, chạy `.exe` (Cách A ở trên).

Phát hành bản tiếp theo: lặp lại với tag mới, vd `v1.0.1`, `v1.1.0`... Muốn thử build mà chưa phát hành
chính thức: vào tab **Actions** → chọn workflow "Build Windows .exe" → **Run workflow** (không cần tag) —
kết quả nằm ở mục **Artifacts** của lần chạy đó thay vì Releases.

### Cấu trúc phần đóng gói (mới, so với bản chỉ chạy qua `Mo_Tool.bat`)

```
desktop_app.py                       # điểm khởi chạy app desktop (mở cửa sổ bằng pywebview)
silent_video_voiceover_tool.spec     # cấu hình PyInstaller — cách đóng gói .exe
gen_icon.py                          # tự tạo icon app (assets/icon.ico, assets/icon.png)
assets/                              # icon đã tạo
.github/workflows/build.yml          # GitHub Actions — build .exe tự động khi push tag
```

### Giới hạn cần biết

- File `.exe` đóng gói khá nặng (ước tính 700MB-1GB) vì kèm cả trình duyệt Chromium — đây là đánh đổi để
  người dùng cuối không cần cài gì.
- Chưa qua bước ký số (code signing) — xem cảnh báo SmartScreen ở Cách A.
- Cơ chế mở cửa sổ (`pywebview`) và bước tải FFmpeg/Chromium trong `build.yml` được thiết kế theo tài liệu
  chính thức của từng thư viện và đã kiểm thử được phần lõi (máy chủ Streamlit khởi động đúng bên trong một
  bản `.exe` đã đóng gói hoàn chỉnh, xem log build) — nhưng **cửa sổ pywebview thật trên Windows và luồng
  build .exe đầy đủ qua GitHub Actions chưa được chạy thử trên Windows thật**, vì môi trường phát triển dự
  án này không có Windows. Nếu build lỗi ở bước nào, xem log chi tiết ở tab Actions và báo lại.

---

## Nhập link YouTube (Gemini Web xem trực tiếp, không cần trích frame)

Mục ② "Chọn video" → chọn **🔗 Nhập link YouTube** (chỉ dùng được khi Engine = Gemini Web).

- Tool gửi link cho Gemini Web để nó **tự xem video và viết kịch bản cho toàn bộ video trong 1 lần**, đồng thời
  **tải video về máy song song** (cần cho bước ghép giọng đọc/xuất video cuối).
- **Kịch bản được lưu ra đĩa ngay khi nhận** (`work/youtube_script.json`). Nếu app bị đóng/sập giữa chừng, mở lại
  và bấm chạy đúng nút cũ: **không tải lại, không hỏi lại Gemini** — chỉ làm tiếp phần còn dở.
- **Chất lượng video tải về** (mặc định **1080p**): tool chỉ cần video để ghép tiếng, KHÔNG cần 4K. Bản 4K trên
  YouTube thường là codec AV1 — giải mã bằng phần mềm rất chậm (đã đo thực tế: video 19 phút 4K mất ~40 phút
  chỉ để quét khoảng lặng, và 28+ phút chưa xong 1 chunk trích frame). 1080p H.264 nhanh gấp nhiều lần.
  Đổi chất lượng → tự tải lại bản mới (file đặt tên theo độ cao, vd `youtube_source_1080p.mp4`); kịch bản đã
  lưu cho cùng link vẫn dùng lại được.
- Không dùng chung được với "Chỉ giữ cảnh hay" (mốc thời gian kịch bản sẽ lệch sau khi video bị cắt).
- Lưu ý pháp lý: tải video YouTube không thuộc quyền của bạn có thể không đúng Điều khoản dịch vụ của YouTube.

## Khi có sự cố: báo cáo chẩn đoán

- Lỗi xảy ra → app tự hiện **khối "📋 Báo cáo lỗi"** (có nút copy ở góc phải khối) và lưu file
  `work/error_report_*.txt`. Copy nguyên khối, dán vào chat.
- Thấy chạy lâu/lạ mà chưa báo lỗi? Mục ④ → mở "🩺 Đang chạy lâu…" → bấm **Tạo báo cáo chẩn đoán** (dùng được khi chưa chạy hoặc sau khi chạy xong/lỗi, không cần chờ crash).
  **Khi pipeline ĐANG chạy thì đừng bấm nút nào trên trang** — thao tác giao diện có thể làm ngắt lần chạy hiện tại
  (log sẽ ghi "Lần chạy bị NGẮT giữa chừng"). Muốn xem tiến độ lúc đang chạy: mở `work/pipeline.log` bằng Notepad.
- Khi trích frame, log hiện tiến độ + thời gian còn lại (`Trích frame 24/96 — 5.3s/khung, còn ~6.4 phút`) —
  nếu thấy con số này quá lớn, nguyên nhân thường là video độ phân giải/codec nặng: chọn nguồn 1080p H.264.

## Đồng bộ lời đọc với hình

Mặc định lời đọc được đặt **đúng mốc thời gian của kịch bản** (ô "Cho phép lời đọc sớm hơn hình tối đa" = 0).
Nếu muốn khoảng nghỉ giữa câu tự nhiên hơn, nâng ô đó lên (tối đa 1 giây): tool được kéo lời đọc sớm lên, nhưng
**không bao giờ vượt mức bạn đặt và không cộng dồn**. Sau mỗi lần chạy, `sync_report.json` ghi độ lệch lớn nhất.

## "Chỉ giữ cảnh hay" chạy nhanh hơn

Bước quét nhanh dùng **một lượt FFmpeg** lấy đúng 1 khung mỗi 6 giây (trước đây OpenCV nhảy từng khung và lấy gấp
đôi số khung — chậm tới hàng chục phút với video AV1/VP9). Log ghi rõ codec của video, và tiến độ quét mỗi ~15 giây.
