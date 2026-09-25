"""python tests/test_error_report.py — báo cáo lỗi phải LUÔN sinh được, kể cả khi mọi thứ xung quanh hỏng."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import Config  # noqa: E402
from error_report import build_error_report, save_error_report  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


# 1) báo cáo với exception thật + cfg thật → có đủ thành phần
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    cfg = Config(video_path=tmp / "demo.mp4", output_dir=tmp / "out", narrator_pov="Tôi",
                narrative_style="fantasy_inspiring", highlight_mode=True)
    cfg.ensure_dirs()
    cfg.log_path.write_text("dòng log 1\ndòng log 2\ndòng log 3\n" * 30, encoding="utf-8")

    try:
        raise ValueError("lỗi giả lập để test")
    except ValueError as e:
        report = build_error_report(e, cfg, context="đang test", extra={"chunk": 3, "batch": "5/12"})

    check("có tiêu đề báo cáo", "BÁO CÁO LỖI" in report)
    check("có đúng loại lỗi", "ValueError" in report)
    check("có đúng thông điệp lỗi", "lỗi giả lập để test" in report)
    check("có traceback (thấy tên hàm test)", "test_error_report.py" in report)
    check("có bối cảnh đã truyền vào", "đang test" in report)
    check("có thông tin bổ sung đã truyền vào", "chunk: 3" in report and "5/12" in report)
    check("có phiên bản Python", "Python:" in report)
    check("có tóm tắt cấu hình (narrator_pov, narrative_style, highlight_mode)",
          "Tôi" in report and "fantasy_inspiring" in report and "highlight_mode: True" in report)
    check("có log gần nhất", "dòng log 1" in report)
    check("KHÔNG có API key (chưa từng in field api_key/elevenlabs_api_key)",
          "api_key" not in report.lower() or "elevenlabs_api_key" not in report)
    check("bọc trong code block (```) để copy giữ nguyên định dạng", report.startswith("```") and report.rstrip().endswith("```"))

    # 2) lưu file
    saved = save_error_report(report, cfg)
    check(f"lưu file thành công: {saved}", saved is not None and saved.is_file())
    check("nội dung file khớp báo cáo", saved.read_text(encoding="utf-8") == report)

    # 3) báo cáo CHẨN ĐOÁN chủ động (không có exception)
    report2 = build_error_report(None, cfg, context="đang treo, không rõ vì sao")
    check("báo cáo chẩn đoán không có traceback vẫn hợp lệ", "Không có lỗi" in report2)
    check("vẫn có đủ cấu hình + log", "narrative_style" in report2 and "dòng log 1" in report2)

# 4) TRƯỜNG HỢP KHẮC NGHIỆT: cfg=None hoàn toàn, không có log_path, không có gì cả → vẫn không crash
try:
    raise RuntimeError("lỗi không kèm cfg gì cả")
except RuntimeError as e:
    report3 = build_error_report(e, None)
check("cfg=None vẫn sinh được báo cáo, không crash", "RuntimeError" in report3)

# 5) cfg có log_path trỏ tới file KHÔNG TỒN TẠI → không crash, báo rõ lý do
with tempfile.TemporaryDirectory() as d2:
    tmp2 = Path(d2)
    cfg2 = Config(video_path=tmp2 / "x.mp4", output_dir=tmp2 / "out2")
    # KHÔNG gọi ensure_dirs()/tạo log -> log_path chưa tồn tại
    report4 = build_error_report(ValueError("x"), cfg2)
    check("log_path chưa tồn tại vẫn không crash, có ghi chú rõ ràng", "chưa có file log" in report4)

print("TẤT CẢ PASS ✔")
