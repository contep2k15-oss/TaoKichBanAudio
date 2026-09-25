"""Giao diện đồ hoạ (GUI) — chạy bằng: `streamlit run app_streamlit.py`, hoặc double-click file `Mo_Tool.bat`
đi kèm (tự kích hoạt venv + mở trình duyệt tới giao diện này, không cần gõ lệnh).

Không cần Internet ngoài việc gọi Gemini/TTS — Streamlit tự mở một máy chủ cục bộ trên chính máy bạn
(http://localhost:8501), không gửi dữ liệu ra ngoài ngoại trừ phần pipeline vốn đã gọi Gemini/TTS.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import streamlit as st

from config import DEFAULT_GEMINI_MODEL, Config
from error_report import build_error_report, save_error_report
from utils import GeminiWebError, PipelineError, WebAuthError, suppress_console_windows

st.set_page_config(page_title="Silent Video Voiceover Creator", page_icon="🎙️", layout="wide")
suppress_console_windows()  # xem utils.py — bắt buộc để tiến trình con của pydub không tự bật console Windows

# ════════════════════════════════════════════════════════════
# State
# ════════════════════════════════════════════════════════════
DEFAULTS = {
    "video_path": "", "youtube_url": "", "output_dir": "", "engine": "web", "browser_channel": "chrome",
    "web_model_hint": "Flash|Fast|Nhanh", "gemini_model": DEFAULT_GEMINI_MODEL, "api_key": "",
    "tts_engine": "edge", "language": "vi", "voice": "", "style": Config.style,
    "narrator_pov": "", "narrative_style": "natural",
    "elevenlabs_api_key": "", "elevenlabs_model": "eleven_multilingual_v2",
    "mode": "interval", "interval": 2.5, "scene_threshold": 0.35,
    "chunk_minutes": 4.0, "bgm_duck": 0.7, "no_bgm": False, "max_speedup": 1.25,
    "pause_range": (0.3, 1.2),
    "highlight_mode": False, "highlight_target_ratio": 0.4, "highlight_frame_interval": 6.0,
    "force": False, "verbose": False, "chrome_proc": None, "result": None, "running": False,
}
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)


def build_cfg(*, stop_after: int | None = None) -> Config:
    out_dir = st.session_state.output_dir.strip() or None
    video = Path(st.session_state.video_path).expanduser() if st.session_state.video_path.strip() else None
    yt_url = st.session_state.youtube_url.strip() or None
    min_pause, max_pause = st.session_state.pause_range
    default_name = video.stem if video else ("_youtube" if yt_url else "_gui")
    return Config(
        video_path=video, youtube_url=yt_url,
        output_dir=Path(out_dir) if out_dir else (Path("output") / default_name),
        engine=st.session_state.engine, browser_channel=st.session_state.browser_channel,
        web_model_hint=st.session_state.web_model_hint, gemini_model=st.session_state.gemini_model or None,
        api_key=st.session_state.api_key or None,
        tts_engine=st.session_state.tts_engine, language=st.session_state.language,
        voice=st.session_state.voice or None, style=st.session_state.style,
        narrator_pov=st.session_state.narrator_pov, narrative_style=st.session_state.narrative_style,
        elevenlabs_api_key=st.session_state.elevenlabs_api_key or None, elevenlabs_model=st.session_state.elevenlabs_model,
        extract_mode=st.session_state.mode, frame_interval_sec=st.session_state.interval,
        scene_threshold=st.session_state.scene_threshold, chunk_target_sec=st.session_state.chunk_minutes * 60.0,
        bgm_duck_ratio=st.session_state.bgm_duck, keep_bgm=not st.session_state.no_bgm,
        max_speedup=st.session_state.max_speedup, min_pause_sec=min_pause, max_pause_sec=max_pause,
        highlight_mode=st.session_state.highlight_mode, highlight_target_ratio=st.session_state.highlight_target_ratio,
        highlight_frame_interval_sec=st.session_state.highlight_frame_interval,
        force=st.session_state.force, stop_after=stop_after,
    )


def browse_file() -> str | None:
    """Mở hộp thoại chọn file NGUYÊN BẢN của hệ điều hành (không phải trình duyệt), chạy trên chính máy
    đang mở Streamlit — hoạt động vì Streamlit ở đây chạy cục bộ (localhost), không phải máy chủ từ xa."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(title="Chọn video",
                                          filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm"), ("Mọi file", "*.*")])
        root.destroy()
        return path or None
    except Exception as e:  # noqa: BLE001
        st.warning(f"Không mở được hộp thoại chọn file ({e}). Hãy dán trực tiếp đường dẫn vào ô bên trên.")
        return None


# ════════════════════════════════════════════════════════════
# Sidebar — cấu hình
# ════════════════════════════════════════════════════════════
with st.sidebar:
    st.header("⚙️ Cấu hình")

    st.subheader("Nguồn phân tích video")
    st.session_state.engine = st.radio("Engine", ["web", "api"], horizontal=True,
                                       format_func=lambda x: "Gemini Web (đăng nhập Google)" if x == "web" else "Gemini API (cần API key)")
    if st.session_state.engine == "web":
        st.session_state.browser_channel = st.selectbox("Trình duyệt", ["chrome", "msedge", "chromium"])
        st.session_state.web_model_hint = st.text_input("Regex model trên giao diện", st.session_state.web_model_hint)
    else:
        st.session_state.api_key = st.text_input("Gemini API key", st.session_state.api_key, type="password")
        st.session_state.gemini_model = st.text_input("Model", st.session_state.gemini_model)

    st.subheader("Giọng đọc (TTS)")
    st.session_state.tts_engine = st.radio("TTS engine", ["edge", "elevenlabs"], horizontal=True,
                                           format_func=lambda x: "Edge-TTS (miễn phí)" if x == "edge" else "ElevenLabs (trả phí)")
    st.session_state.language = st.selectbox("Ngôn ngữ", ["vi", "en"])
    st.session_state.voice = st.text_input("Giọng (để trống = mặc định)", st.session_state.voice)
    if st.session_state.tts_engine == "elevenlabs":
        st.session_state.elevenlabs_api_key = st.text_input("ElevenLabs API key", st.session_state.elevenlabs_api_key, type="password")
        st.session_state.elevenlabs_model = st.text_input("ElevenLabs model", st.session_state.elevenlabs_model)

    st.subheader("Kịch bản")
    pov_options = ["Để Gemini tự chọn", "Tôi", "Chúng ta", "Người quan sát", "Nhân vật chính trong video", "Tuỳ chỉnh..."]
    pov_current = st.session_state.narrator_pov
    pov_choice = st.selectbox("Ngôi kể / xưng hô", pov_options,
                              index=pov_options.index(pov_current) if pov_current in pov_options else
                              (5 if pov_current else 0))
    if pov_choice == "Tuỳ chỉnh...":
        st.session_state.narrator_pov = st.text_input("Ngôi kể tuỳ chỉnh", pov_current if pov_current not in pov_options else "")
    elif pov_choice == "Để Gemini tự chọn":
        st.session_state.narrator_pov = ""
    else:
        st.session_state.narrator_pov = pov_choice

    style_labels = {"natural": "Tự nhiên, đời thường", "humorous": "Hài hước, dí dỏm", "formal": "Trang trọng, chuyên nghiệp",
                    "fantasy_inspiring": "Kể chuyện giả tưởng & Truyền cảm hứng"}
    st.session_state.narrative_style = st.selectbox("Phong cách kể chuyện", list(style_labels), index=list(style_labels).index(
        st.session_state.narrative_style), format_func=lambda k: style_labels[k])
    if st.session_state.narrative_style == "fantasy_inspiring":
        st.caption("💡 Video sẽ mở đầu bằng một câu chuyện giả tưởng ngắn để dẫn dắt, rồi thỉnh thoảng nhắc lại "
                  "xuyên suốt để giữ mạch cảm xúc.")
    st.session_state.style = st.text_area("Mô tả phong cách chi tiết (tự do)", st.session_state.style, height=70)

    st.subheader("🎬 Chỉ giữ cảnh hay")
    st.session_state.highlight_mode = st.checkbox("Chỉ giữ lại những cảnh đắt giá và thêm thuyết minh, cắt bỏ phần thừa",
                                                  st.session_state.highlight_mode)
    if st.session_state.highlight_mode:
        st.session_state.highlight_target_ratio = st.slider("Giữ lại khoảng bao nhiêu % video gốc", 0.1, 0.9,
                                                             st.session_state.highlight_target_ratio, 0.05)
        st.caption("Quét nhanh toàn video để chọn đoạn hay TRƯỚC KHI xử lý chi tiết — nhanh và ít tốn hơn nhiều so "
                  "với xử lý toàn bộ video rồi mới cắt.")

    with st.expander("Tuỳ chọn nâng cao"):
        st.session_state.mode = st.radio("Chia cảnh", ["interval", "scene"], horizontal=True)
        st.session_state.interval = st.slider("Khoảng lấy mẫu (giây)", 1.0, 6.0, st.session_state.interval, 0.5)
        st.session_state.chunk_minutes = st.slider("Độ dài mỗi chunk (phút) — video dài", 1.0, 15.0, st.session_state.chunk_minutes, 0.5)
        st.session_state.max_speedup = st.slider("Tăng tốc audio tối đa", 1.0, 1.5, st.session_state.max_speedup, 0.05)
        st.session_state.pause_range = st.slider("Khoảng nghỉ ngẫu nhiên giữa các câu (giây)", 0.0, 3.0,
                                                 st.session_state.pause_range, 0.05,
                                                 help="Chỉ RÚT NGẮN khoảng trống tự nhiên đã có giữa 2 câu, không "
                                                 "bao giờ thêm khoảng lặng mới hay làm lệch hình ảnh.")
        st.session_state.no_bgm = st.checkbox("Bỏ hẳn âm thanh gốc", st.session_state.no_bgm)
        if not st.session_state.no_bgm:
            st.session_state.bgm_duck = st.slider("Âm lượng nhạc nền khi có giọng đọc", 0.1, 1.0, st.session_state.bgm_duck, 0.05)
        st.session_state.output_dir = st.text_input("Thư mục kết quả (để trống = tự đặt theo tên video)", st.session_state.output_dir)
        st.session_state.force = st.checkbox("Bỏ qua checkpoint, làm lại từ đầu (--force)", st.session_state.force)
        st.session_state.verbose = st.checkbox("Log chi tiết (DEBUG)", st.session_state.verbose)

st.title("🎙️ Silent Video Voiceover Creator")
st.caption("Video không lời → Gemini viết kịch bản → TTS đọc → ghép khớp timeline. Hỗ trợ video dài, tự resume nếu bị gián đoạn.")

# ════════════════════════════════════════════════════════════
# Khu vực 1: đăng nhập Gemini Web (chỉ hiện khi engine=web)
# ════════════════════════════════════════════════════════════
if st.session_state.engine == "web":
    st.subheader("① Đăng nhập Gemini Web")
    login_cfg = build_cfg()
    login_cfg.user_data_dir.mkdir(parents=True, exist_ok=True)
    col1, col2, col3 = st.columns([1, 1, 2])

    if col1.button("🌐 Mở Chrome để đăng nhập"):
        from gemini_web_driver import find_browser_executable
        exe = find_browser_executable(login_cfg.browser_channel)
        if not exe:
            st.error(f"Không tìm thấy trình duyệt '{login_cfg.browser_channel}'. Hãy cài Google Chrome.")
        else:
            cmd = [exe, f"--user-data-dir={login_cfg.user_data_dir}", "--no-first-run",
                  "--no-default-browser-check", "--password-store=basic", login_cfg.gemini_url]
            st.session_state.chrome_proc = subprocess.Popen(cmd)
            st.info("Đã mở Chrome — đăng nhập tài khoản Google trong cửa sổ vừa mở, xong thì đóng cửa sổ đó lại rồi bấm nút bên cạnh.")

    if col2.button("✅ Tôi đã đăng nhập xong, kiểm tra lại"):
        from gemini_web_driver import check_session
        with st.spinner("Đang kiểm tra phiên đăng nhập (chạy ẩn)..."):
            try:
                ok = check_session(login_cfg)
            except GeminiWebError as e:
                ok, err = False, str(e)
            else:
                err = None
        if ok:
            st.success("✔ Phiên đăng nhập hợp lệ — các lần sau không cần đăng nhập lại.")
        else:
            st.error("✖ Chưa thấy phiên đăng nhập hợp lệ." + (f" ({err})" if err else " Hãy chắc chắn đã đăng nhập xong rồi mới đóng cửa sổ Chrome, rồi thử lại."))
    st.divider()

# ════════════════════════════════════════════════════════════
# Khu vực 2: chọn video
# ════════════════════════════════════════════════════════════
st.subheader("② Chọn video")
source_mode = st.radio("Nguồn video", ["upload", "youtube"], horizontal=True, key="source_mode",
                       format_func=lambda x: "📁 Tải video lên" if x == "upload" else "🔗 Nhập link YouTube")

if source_mode == "upload":
    st.session_state.youtube_url = ""
    c1, c2 = st.columns([4, 1])
    st.session_state.video_path = c1.text_input("Đường dẫn video", st.session_state.video_path,
                                                placeholder=r"C:\Users\Ban\Videos\demo.mp4", label_visibility="collapsed")
    if c2.button("📂 Duyệt file...", use_container_width=True):
        picked = browse_file()
        if picked:
            st.session_state.video_path = picked
            st.rerun()

    video_ok = bool(st.session_state.video_path) and Path(st.session_state.video_path).is_file()
    if st.session_state.video_path and not video_ok:
        st.error(f"Không tìm thấy file: {st.session_state.video_path}")
    elif video_ok:
        st.caption(f"✔ {Path(st.session_state.video_path).name} ({Path(st.session_state.video_path).stat().st_size / 1e6:.1f} MB)")
else:
    st.session_state.video_path = ""
    if st.session_state.engine != "web":
        st.warning("Chế độ link YouTube chỉ hoạt động với **Gemini Web (đăng nhập)** — hãy đổi Engine ở "
                  "thanh bên trái sang 'Gemini Web' trước.")
    st.session_state.youtube_url = st.text_input("Link YouTube", st.session_state.youtube_url,
                                                  placeholder="https://www.youtube.com/watch?v=...")
    from youtube_source import is_youtube_url
    video_ok = bool(st.session_state.youtube_url) and is_youtube_url(st.session_state.youtube_url) and st.session_state.engine == "web"
    if st.session_state.youtube_url and not is_youtube_url(st.session_state.youtube_url):
        st.error("Link không hợp lệ — cần dạng youtube.com/watch?v=..., youtu.be/... hoặc .../shorts/...")
    elif video_ok:
        st.caption("✔ Link hợp lệ. Gemini Web sẽ xem trực tiếp video này để viết kịch bản (không cần trích "
                  "frame cục bộ) — video vẫn được tải về máy song song để phục vụ ghép/xuất video cuối.")
        st.caption("⚠️ Độ tin cậy với video RẤT DÀI chưa được kiểm chứng đầy đủ — nếu kịch bản bị thiếu/cụt, "
                  "thử lại hoặc chuyển sang cách tải video lên.")

st.divider()

# ════════════════════════════════════════════════════════════
# Khu vực 3: trạng thái hiện có (đọc, KHÔNG chạy gì) — biết ngay đang xử lý tới đâu trước khi bấm nút nào
# ════════════════════════════════════════════════════════════
status: object | None = None
if video_ok:
    try:
        import pipeline_status
        cfg_peek = build_cfg()
        status = pipeline_status.read_status(cfg_peek)
    except Exception:  # noqa: BLE001 — chỉ để xem trước, lỗi ở đây không được chặn phần còn lại của trang
        status = None

    if status is not None:
        st.subheader("③ Tiến độ hiện có")
        st.info(status.summary_line())
        from utils import format_timestamp as _fmt_ts
        rows = [{"Chunk": c.index + 1, "Thời gian": f"{_fmt_ts(c.start_sec)} → {_fmt_ts(c.end_sec)}",
                "Trích frame": "✅" if c.has(pipeline_status.Stage.EXTRACTED) else "—",
                "Kịch bản": "✅" if c.has(pipeline_status.Stage.SCRIPT) else "—",
                "Giọng đọc": "✅" if c.has(pipeline_status.Stage.TTS) else "—",
                "Đã ghép": "✅" if c.has(pipeline_status.Stage.ASSEMBLED) else "—"} for c in status.chunks]
        st.dataframe(rows, hide_index=True, use_container_width=True)
        st.caption("Bảng này chỉ để XEM — không chạy gì. Bấm nút giai đoạn còn thiếu ở mục ④ bên dưới để tiếp tục "
                  "đúng chỗ dở dang, không cần làm lại từ đầu.")
        st.divider()

# ════════════════════════════════════════════════════════════
# Khu vực 4: chạy pipeline — TÁCH RIÊNG từng giai đoạn để chủ động dừng/tiếp tục, tránh 1 lỗi giữa chừng
# phải chờ chạy lại từ đầu (mọi nút đều tự động RESUME từ checkpoint, bỏ qua phần đã xong)
# ════════════════════════════════════════════════════════════
st.subheader("④ Chạy")
with st.expander("🩺 Đang chạy lâu/thấy lạ mà chưa báo lỗi gì? Tạo báo cáo chẩn đoán để gửi Claude"):
    st.caption("Bấm nút bên dưới bất cứ lúc nào (kể cả khi chưa gặp lỗi) — chụp lại đúng trạng thái/cấu "
              "hình/log hiện tại, không phải đợi crash mới có gì để gửi.")
    if st.button("Tạo báo cáo chẩn đoán ngay bây giờ"):
        try:
            cfg_now = build_cfg()
        except Exception:  # noqa: BLE001 — video/tham số hiện tại có thể chưa hợp lệ, vẫn cứ báo cáo được phần còn lại
            cfg_now = None
        diag = build_error_report(None, cfg_now, context="Người dùng chủ động tạo báo cáo chẩn đoán (chưa chắc đã có lỗi)")
        saved = save_error_report(diag, cfg_now)
        st.code(diag, language="text")
        if saved:
            st.caption(f"Đã lưu vào: `{saved}`")
st.caption("Có thể bấm từng nút một, theo đúng thứ tự ①→④, để kiểm tra kết quả mỗi bước trước khi sang bước "
          "kế — hoặc bấm thẳng nút cuối để chạy hết một lượt. Mọi nút đều tự tiếp tục từ chỗ dở dang, không "
          "làm lại phần đã xong.")
b1, b2, b3, b4 = st.columns(4)
run_plan = b1.button("① Phân đoạn", disabled=not video_ok or st.session_state.running, use_container_width=True,
                     help="Chỉ chia video thành các đoạn (chunk) an toàn — chưa gọi Gemini, chưa tốn TTS.")
run_script_only = b2.button("② Kịch bản", disabled=not video_ok or st.session_state.running, use_container_width=True,
                            help="Trích frame + gọi Gemini viết lời thuyết minh cho mọi chunk — DỪNG trước khi tốn TTS.")
run_tts_only = b3.button("③ Giọng đọc", disabled=not video_ok or st.session_state.running, use_container_width=True,
                         help="Tổng hợp giọng đọc cho mọi đoạn đã có kịch bản — DỪNG trước khi ghép video.")
run_full = b4.button("④ Hoàn tất", type="primary", disabled=not video_ok or st.session_state.running, use_container_width=True,
                     help="Ghép & xuất video hoàn chỉnh. Tự làm nốt mọi bước còn thiếu trước đó nếu cần.")

if run_plan or run_script_only or run_tts_only or run_full:
    st.session_state.running = True
    st.session_state.result = None
    import long_video_pipeline
    from utils import setup_logging

    stop_after = 0 if run_plan else (2 if run_script_only else (3 if run_tts_only else None))
    cfg = build_cfg(stop_after=stop_after)
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    setup_logging(cfg.log_path, st.session_state.verbose)

    status_box = st.status("Đang xử lý...", expanded=True)
    chunk_bar = st.progress(0.0, text="")

    def on_step(n: int, title: str) -> None:
        status_box.write(f"**Bước {n}** — {title}")

    def on_chunk_progress(i: int, n: int) -> None:
        chunk_bar.progress(i / n, text=f"Chunk {i}/{n}")

    def _show_error_report(exc: Exception, context: str) -> None:
        """Sinh báo cáo lỗi ĐẦY ĐỦ, hiện trong khung có nút copy sẵn của Streamlit (góc trên-phải khối mã)
        — người dùng chỉ cần bấm copy, dán thẳng vào chat gửi Claude, không cần tự tìm log/traceback."""
        report = build_error_report(exc, cfg, context=context)
        saved = save_error_report(report, cfg)
        st.markdown("**📋 Báo cáo lỗi (bấm biểu tượng copy ở góc khối bên dưới, dán thẳng vào chat để gửi):**")
        st.code(report, language="text")
        if saved:
            st.caption(f"Đã lưu báo cáo vào: `{saved}` — vẫn tìm lại được nếu bạn lỡ đóng trang này.")

    try:
        t0 = time.perf_counter()
        out = long_video_pipeline.run(cfg, on_step=on_step, on_chunk_progress=on_chunk_progress, interactive=False)
        elapsed = time.perf_counter() - t0
        stage_labels = {0: "Đã phân đoạn xong", 2: "Đã có kịch bản", 3: "Đã có giọng đọc"}
        status_box.update(label=f"{stage_labels.get(stop_after, 'Hoàn tất')} trong {elapsed:.0f}s ✅", state="complete")
        st.session_state.result = {"cfg_workdir": str(cfg.workdir), "output_dir": str(cfg.output_dir),
                                   "script": str(out["script"]) if out["script"] else None,
                                   "voiceover": str(out["voiceover"]) if out["voiceover"] else None,
                                   "video": str(out["video"]) if out["video"] else None,
                                   "log": cfg.log_path.read_text(encoding="utf-8", errors="replace") if cfg.log_path.is_file() else ""}
        if stop_after is not None:
            st.info("Dừng đúng theo giai đoạn đã chọn. Xem lại bảng tiến độ ở mục ③ (tải lại trang hoặc bấm "
                    "lại một nút bất kỳ để cập nhật bảng), rồi bấm nút giai đoạn tiếp theo khi sẵn sàng.")
    except (WebAuthError, GeminiWebError) as e:
        status_box.update(label="Cần đăng nhập lại", state="error")
        st.error(f"{e}\n\nHãy đăng nhập lại ở mục ① phía trên rồi chạy lại — chỉ cần bấm lại ĐÚNG nút vừa "
                "chạy, không cần làm lại các chunk đã xong trước đó.")
        _show_error_report(e, "Lỗi đăng nhập/phiên Gemini Web")
    except PipelineError as e:
        status_box.update(label="Có lỗi xảy ra", state="error")
        st.error(f"{e}\n\nBấm lại ĐÚNG nút vừa chạy để tiếp tục từ chỗ dở dang — không cần làm lại từ đầu.")
        _show_error_report(e, "Lỗi trong pipeline (PipelineError)")
    except Exception as e:  # noqa: BLE001
        status_box.update(label="Lỗi không lường trước", state="error")
        st.exception(e)
        _show_error_report(e, "Lỗi không lường trước (Exception)")
    finally:
        st.session_state.running = False

# ════════════════════════════════════════════════════════════
# Kết quả
# ════════════════════════════════════════════════════════════
result = st.session_state.result
if result:
    st.divider()
    st.subheader("⑤ Kết quả")
    if result["video"] and Path(result["video"]).is_file():
        left, right = st.columns([3, 2])
        with left:
            st.video(result["video"])
            st.download_button("⬇️ Tải video đã thuyết minh", Path(result["video"]).read_bytes(),
                               Path(result["video"]).name, "video/mp4")
        with right:
            if result["voiceover"] and Path(result["voiceover"]).is_file():
                st.download_button("⬇️ Tải final_voiceover.mp3", Path(result["voiceover"]).read_bytes(),
                                   Path(result["voiceover"]).name, "audio/mpeg")
            if result["script"] and Path(result["script"]).is_file():
                st.download_button("⬇️ Tải script.json", Path(result["script"]).read_bytes(),
                                   Path(result["script"]).name, "application/json")
    elif result["script"] and Path(result["script"]).is_file():
        st.info("Đã tạo xong kịch bản. Mở file, sửa nếu cần, rồi bấm '🚀 Tạo video thuyết minh' để chạy tiếp "
               "(sẽ tự dùng lại kịch bản này, không tốn công gọi lại Gemini).")
        st.download_button("⬇️ Tải script.json để xem/sửa", Path(result["script"]).read_bytes(),
                           Path(result["script"]).name, "application/json")
        st.json(__import__("json").loads(Path(result["script"]).read_text(encoding="utf-8")), expanded=False)
    st.caption(f"Thư mục kết quả: `{result['output_dir']}` — thư mục làm việc/log: `{result['cfg_workdir']}`")
    with st.expander("Nhật ký chi tiết"):
        st.code(result["log"], language="text")
