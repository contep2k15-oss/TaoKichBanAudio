"""python tests/test_checkpoint.py"""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checkpoint import PipelineCheckpoint, Stage  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


with tempfile.TemporaryDirectory() as d:
    workdir = Path(d)
    fp = {"video": "v.mp4", "size": 123, "language": "vi"}

    # 1) checkpoint mới toanh: chưa có gì
    cp = PipelineCheckpoint(workdir, fp)
    check("chunk mới: next_stage = EXTRACTED", cp.next_stage(0) is Stage.EXTRACTED)
    check("chunk mới: chưa done", not cp.chunk_done(0))

    # 2) lưu tuần tự 4 giai đoạn cho chunk 0
    cp.save(0, Stage.EXTRACTED, {"frames": ["a.jpg", "b.jpg"]})
    check("sau EXTRACTED: next_stage = SCRIPT", cp.next_stage(0) is Stage.SCRIPT)
    cp.save(0, Stage.SCRIPT, {"segments": [{"id": 1, "text": "xin chào"}]})
    cp.save(0, Stage.TTS, {"results": [{"id": 1, "status": "ok"}]})
    check("chưa ASSEMBLED: chunk chưa done", not cp.chunk_done(0))
    cp.save(0, Stage.ASSEMBLED, {"audio_path": "chunk0000.mp3"})
    check("đủ 4 giai đoạn: chunk_done = True", cp.chunk_done(0))
    check("load lại đúng dữ liệu đã lưu", cp.load(0, Stage.SCRIPT)["segments"][0]["text"] == "xin chào")

    # 3) chunk 1 mới làm được nửa chừng
    cp.save(1, Stage.EXTRACTED, {"frames": []})
    check("chunk 1 dở dang: next_stage = SCRIPT", cp.next_stage(1) is Stage.SCRIPT)
    check("resume_summary (1 chunk done, 3 tổng): đúng '1/3'", "1/3" in cp.resume_summary(3))  # chunk1 dở dang không tính là done

    # 4) MÔ PHỎNG "rớt mạng giữa chừng, khởi động lại": tạo instance MỚI trỏ cùng workdir/fingerprint
    cp2 = PipelineCheckpoint(workdir, fp)
    check("mở lại (giả lập restart process): chunk 0 vẫn done", cp2.chunk_done(0))
    check("mở lại: chunk 1 vẫn nhớ đang ở SCRIPT (không phải làm lại EXTRACTED)", cp2.next_stage(1) is Stage.SCRIPT)
    check("mở lại: chunk 2 (chưa đụng tới) vẫn ở EXTRACTED", cp2.next_stage(2) is Stage.EXTRACTED)
    check("resume_summary: 1/3 chunk đã xong hoàn toàn", cp2.resume_summary(3) == "Resume: 1/3 chunk đã xong, tiếp tục từ chunk #1.")

    # 5) đổi FINGERPRINT (vd đổi video khác / đổi ngôn ngữ) → toàn bộ checkpoint cũ bị vô hiệu, không dùng nhầm
    cp3 = PipelineCheckpoint(workdir, {**fp, "language": "en"})
    check("fingerprint đổi → chunk 0 coi như CHƯA làm (thư mục checkpoint riêng, không dùng nhầm)", not cp3.chunk_done(0))
    check("fingerprint đổi → next_stage về lại EXTRACTED", cp3.next_stage(0) is Stage.EXTRACTED)
    cp3.save(0, Stage.EXTRACTED, {"frames": ["khac.jpg"]})   # tiến độ RIÊNG của fingerprint "en", không đụng tới "vi"

    # 6) quay lại fingerprint BAN ĐẦU (vi) → checkpoint cũ của nó vẫn CÒN NGUYÊN, không bị cp3 (en) xoá mất
    cp4 = PipelineCheckpoint(workdir, fp)
    check("quay lại fingerprint cũ → chunk 0 vẫn done (không bị fingerprint khác xoá mất)", cp4.chunk_done(0))
    check("hai fingerprint dùng hai thư mục checkpoint khác nhau", cp3.dir != cp4.dir)

    # 7) invalidate_from: làm lại từ một giai đoạn giữa chừng (vd --force cho riêng 1 chunk)
    cp4.invalidate_from(0, Stage.TTS)
    check("invalidate_from(TTS): mất TTS+ASSEMBLED, còn EXTRACTED+SCRIPT", cp4.next_stage(0) is Stage.TTS and not cp4.chunk_done(0))
    check("EXTRACTED vẫn còn nguyên sau invalidate_from(TTS)", cp4.has(0, Stage.EXTRACTED) and cp4.has(0, Stage.SCRIPT))

    # 8) checkpoints/00_chunks.json: kế hoạch chia chunk được lưu & đọc lại đúng fingerprint
    check("chưa lưu plan → load_plan() = None", cp4.load_plan() is None)
    cp4.save_plan([{"index": 0, "start_sec": 0, "end_sec": 240}])
    check("đã lưu plan → đọc lại đúng nội dung", cp4.load_plan()[0]["end_sec"] == 240)
    cp5 = PipelineCheckpoint(workdir, {**fp, "language": "en"})   # fingerprint khác
    check("fingerprint khác → load_plan() = None (không dùng nhầm kế hoạch cũ)", cp5.load_plan() is None)

print("TẤT CẢ PASS ✔")
