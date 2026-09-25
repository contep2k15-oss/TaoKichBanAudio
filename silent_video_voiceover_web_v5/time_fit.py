"""Cơ chế đồng bộ thời gian: quyết định có cần time-stretch (FFmpeg atempo) hay không, và mức độ.

Đây là một HÀM THUẦN (pure function) — không đụng tới file/FFmpeg/mạng — để có thể unit-test độc lập,
tách khỏi phần I/O nặng (đo file thật, chạy atempo) ở `tts_engine.py`.

Công thức cốt lõi mà đề bài yêu cầu:
    r = actual_duration / target_duration     (tỉ lệ TTS cần "nén" lại bao nhiêu để vừa khung cảnh)
    speed = clamp(r, 1.0, max_speedup)         (không bao giờ làm CHẬM giọng đọc, chỉ tăng tốc)
Nếu r > max_speedup (đã tăng tốc hết cỡ 1.25× vẫn dài):
    1) Cho lấn (gap-fill) vào khoảng trống trước đoạn kế tiếp (available_sec), MIỄN LÀ không vượt quá đó.
    2) Nếu vẫn vượt cả khoảng trống đó:
         - allow_video_retime=False (mặc định): đánh dấu 'clipped' — audio bị cắt cụt phần dư (bước
           audio_assembler.py sẽ cắt + fade-out), pipeline vẫn chạy tiếp tự động.
         - allow_video_retime=True: đánh dấu 'needs_video_retime' — pipeline sẽ NHỜ video_retime.py làm
           chậm khung hình video trong đúng cửa sổ đó lại để có thêm chỗ chứa audio, thay vì cắt lời thoại.
"""
from __future__ import annotations

from dataclasses import dataclass

EPS_SEC = 0.02


@dataclass(frozen=True)
class FitPlan:
    ratio: float          # r = actual_sec / target_sec (trước khi giới hạn) — để log/báo cáo
    speed: float          # hệ số atempo thực sự sẽ áp dụng (1.0 = giữ nguyên)
    final_sec: float      # thời lượng audio SAU khi áp speed (ước tính; audio thật đo lại sau khi export)
    status: str           # ok | sped_up | overflow | clipped | needs_video_retime
    retime_factor: float = 1.0   # >1: video cần được LÀM CHẬM (kéo dài) theo hệ số này để đủ chỗ chứa audio


def compute_fit(actual_sec: float, target_sec: float, available_sec: float, *,
                max_speedup: float = 1.25, allow_video_retime: bool = False) -> FitPlan:
    """
    actual_sec    : thời lượng thật của audio TTS đã tổng hợp (đo bằng ffprobe/pydub).
    target_sec    : thời lượng "bối cảnh gốc" — khung thời gian mà kịch bản dự định đoạn này chiếm.
    available_sec : thời lượng TỐI ĐA có thể lấn tới (thường là tới điểm bắt đầu của đoạn kế tiếp, hoặc
                    hết video). Luôn ≥ target_sec.
    """
    target = max(target_sec, 0.3)          # tránh chia cho 0 với đoạn cực ngắn
    available = max(available_sec, target)
    ratio = actual_sec / target

    if actual_sec <= target + EPS_SEC:
        return FitPlan(ratio=ratio, speed=1.0, final_sec=actual_sec, status="ok")

    if ratio <= max_speedup:
        speed = min(max_speedup, ratio * 1.003)     # nhích thêm 0.3% cho chắc chắn vừa khít, tránh lệch làm tròn
        return FitPlan(ratio=ratio, speed=speed, final_sec=actual_sec / speed, status="sped_up")

    # Tăng tốc tối đa (1.25×) vẫn không đủ:
    speed = max_speedup
    final_sec = actual_sec / speed
    if final_sec <= available + EPS_SEC:
        return FitPlan(ratio=ratio, speed=speed, final_sec=final_sec, status="overflow")   # lấn khoảng trống, vẫn vừa
    if allow_video_retime:
        retime_factor = final_sec / available                                             # video cần dài thêm bấy nhiêu lần
        return FitPlan(ratio=ratio, speed=speed, final_sec=final_sec, status="needs_video_retime",
                       retime_factor=retime_factor)
    return FitPlan(ratio=ratio, speed=speed, final_sec=final_sec, status="clipped")        # đành cắt bớt cuối câu
