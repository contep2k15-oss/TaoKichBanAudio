"""python tests/test_highlight_selector.py — kiểm thử hàm chọn lọc highlight (thuần, không I/O)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from highlight_selector import _coerce_highlight, build_highlight_prompt, select_highlights  # noqa: E402


def check(name, cond):
    assert cond, name
    print("  ✓", name)


# 1) chọn đúng các đoạn ĐIỂM CAO NHẤT trước, dừng khi đạt ngân sách
cands = [(0.0, 10.0, 3.0), (20.0, 30.0, 9.0), (40.0, 50.0, 7.0), (60.0, 70.0, 5.0)]
chosen = select_highlights(cands, target_total_sec=25.0)
# Ưu tiên điểm cao trước (9, 7), rồi LẤP ĐẦY phần ngân sách còn dư (5s) bằng đoạn điểm kế tiếp (5đ, cắt còn 5s)
check(f"chọn theo điểm cao dần, lấp đầy ngân sách còn dư bằng đoạn kế tiếp (cắt vừa đủ): {chosen}",
      chosen == [(20.0, 30.0), (40.0, 50.0), (60.0, 65.0)])
check("KHÔNG chọn đoạn điểm thấp nhất (3đ) vì ngân sách đã hết", (0.0, 10.0) not in chosen)
total1 = sum(e - s for s, e in chosen)
check(f"tổng đúng bằng ngân sách (đã lấp đầy tối đa): {total1}s == 25.0s", total1 == 25.0)

# 2) đoạn cuối bị CẮT vừa đủ ngân sách còn lại (không vượt quá target)
cands2 = [(0.0, 10.0, 9.0), (20.0, 40.0, 8.0)]  # đoạn 2 dài 20s nhưng ngân sách chỉ còn 15s sau đoạn 1
chosen2 = select_highlights(cands2, target_total_sec=25.0)
check(f"đoạn 2 bị cắt còn đúng phần ngân sách dư (10s + 15s = 25s): {chosen2}",
      chosen2 == [(0.0, 10.0), (20.0, 35.0)])
total2 = sum(e - s for s, e in chosen2)
check(f"tổng thời lượng KHÔNG vượt quá target ({total2}s ≤ 25s)", total2 <= 25.0)

# 3) kết quả LUÔN sắp theo THỜI GIAN (không theo điểm số) — giữ mạch tự sự gốc
cands3 = [(100.0, 110.0, 9.0), (10.0, 20.0, 5.0), (50.0, 60.0, 7.0)]
chosen3 = select_highlights(cands3, target_total_sec=100.0)
check(f"kết quả sắp theo thời gian tăng dần dù điểm số ngược lại: {chosen3}",
      chosen3 == sorted(chosen3, key=lambda w: w[0]))

# 4) các đoạn CHỒNG LẤN nhau → chỉ giữ đoạn điểm cao hơn, bỏ đoạn chồng lấn điểm thấp
cands4 = [(10.0, 20.0, 9.0), (15.0, 25.0, 6.0), (30.0, 40.0, 8.0)]  # đoạn 2 chồng lấn đoạn 1
chosen4 = select_highlights(cands4, target_total_sec=100.0)
check(f"đoạn chồng lấn điểm thấp hơn bị loại, chỉ còn 2 đoạn không chồng: {chosen4}",
      chosen4 == [(10.0, 20.0), (30.0, 40.0)])

# 5) không có ứng viên nào / target=0 → trả về rỗng (không cắt gì, giữ nguyên video gốc)
check("không có ứng viên → []", select_highlights([], 100.0) == [])
check("target=0 → []", select_highlights(cands, 0.0) == [])

# 6) ứng viên có end<=start (dữ liệu Gemini lỗi) → tự động loại bỏ, không crash
cands6 = [(10.0, 10.0, 9.0), (5.0, 3.0, 8.0), (20.0, 30.0, 7.0)]
chosen6 = select_highlights(cands6, target_total_sec=100.0)
check(f"đoạn có end≤start bị loại tự động, chỉ còn đoạn hợp lệ: {chosen6}", chosen6 == [(20.0, 30.0)])

# ── _coerce_highlight: bóc tách + kẹp trong phạm vi [t0,t1] ──
h = _coerce_highlight({"start_time": "00:00:05.000", "end_time": "00:00:15.000", "score": 8}, t0=0.0, t1=20.0)
check("bóc tách đúng, score giữ nguyên trong [1,10]", h == (5.0, 15.0, 8.0))

h2 = _coerce_highlight({"start_time": "00:00:05.000", "end_time": "00:00:15.000", "score": 99}, t0=0.0, t1=20.0)
check("score vượt 10 → kẹp về 10", h2[2] == 10.0)

h3 = _coerce_highlight({"start_time": "00:00:25.000", "end_time": "00:00:35.000", "score": 8}, t0=0.0, t1=20.0)
check("khoảng nằm ngoài [t0,t1] hoàn toàn → loại bỏ (None)", h3 is None)

h4 = _coerce_highlight({"start_time": "00:00:15.000", "end_time": "00:00:35.000", "score": 8}, t0=0.0, t1=20.0)
check("khoảng lấn ra ngoài → tự kẹp về đúng [t0,t1]", h4 == (15.0, 20.0, 8.0))

h5 = _coerce_highlight({"start_time": "khong hop le"}, t0=0.0, t1=20.0)
check("dữ liệu hỏng → None, không crash", h5 is None)

# ── build_highlight_prompt: nội dung cơ bản đúng ──
p = build_highlight_prompt(__import__("config").Config(video_path=None, output_dir="/tmp/hx"), [2.0, 8.0], 0.0, 12.0, 60.0)
check("prompt chứa mốc thời gian khoảng đang xét", "00:00:00.000" in p and "00:00:12.000" in p)
check("prompt yêu cầu định dạng JSON có 'score'", '"score"' in p)
check("prompt cho phép trả về RỖNG khi không có gì nổi bật", "RỖNG" in p)

# 7) hai đoạn CHẠM SÁT NHAU (đoạn này kết thúc đúng lúc đoạn kia bắt đầu) → KHÔNG bị coi là chồng lấn,
#    cả hai đều được giữ (vùng "hay" trải dài qua ranh giới 2 lô quét liền kề)
cands7 = [(10.0, 12.0, 9.0), (12.0, 18.0, 9.0), (18.0, 20.0, 9.0)]
chosen7 = select_highlights(cands7, target_total_sec=100.0)
check(f"2 đoạn chạm sát nhau KHÔNG bị loại, cả 3 đoạn liền mạch đều được giữ: {chosen7}",
      chosen7 == [(10.0, 12.0), (12.0, 18.0), (18.0, 20.0)])

print("TẤT CẢ PASS ✔")
