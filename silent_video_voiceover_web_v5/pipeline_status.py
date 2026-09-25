"""Đọc trạng thái checkpoint HIỆN CÓ của một video — CHỈ ĐỌC, không chạy hay thay đổi gì — để hiển thị
"đang xử lý tới đâu" trước khi quyết định làm tiếp giai đoạn nào. Dùng chung cho GUI (app_streamlit.py)
và CLI (`main.py --status`).

Đây là câu trả lời trực tiếp cho vấn đề "một luồng lỗi phải làm lại toàn bộ": pipeline vốn đã tách thành
4 giai đoạn checkpoint riêng cho TỪNG macro-chunk (xem checkpoint.py), và tự động resume đúng chỗ dở dang —
nhưng người dùng không có cách nào NHÌN THẤY trạng thái đó ngoài đọc log thô. Module này phơi bày trạng thái
đó ra thành dữ liệu có cấu trúc, để GUI vẽ thành bảng, và người dùng biết chính xác nên bấm nút giai đoạn
nào tiếp theo thay vì đoán hoặc bấm "chạy lại từ đầu" một cách không cần thiết.
"""
from __future__ import annotations

from dataclasses import dataclass

from checkpoint import ALL_STAGES, PipelineCheckpoint, Stage
from config import Config
from long_video_pipeline import build_fingerprint
from utils import PipelineError, format_timestamp, probe_media


@dataclass
class ChunkStatus:
    index: int
    start_sec: float
    end_sec: float
    stages_done: set[str]

    def has(self, stage: Stage) -> bool:
        return stage.value in self.stages_done

    @property
    def done(self) -> bool:
        """Chunk đã đi qua đủ CẢ 4 giai đoạn (trích frame → kịch bản → giọng đọc → đã ghép)."""
        return self.has(Stage.ASSEMBLED)

    @property
    def next_stage_label(self) -> str:
        for s, label in ((Stage.EXTRACTED, "Trích frame"), (Stage.SCRIPT, "Tạo kịch bản"),
                         (Stage.TTS, "Tạo giọng đọc"), (Stage.ASSEMBLED, "Ghép audio")):
            if not self.has(s):
                return label
        return "Đã xong"


@dataclass
class PipelineStatus:
    video_duration_label: str
    chunks: list[ChunkStatus]

    @property
    def n_done(self) -> int:
        return sum(1 for c in self.chunks if c.done)

    @property
    def n_total(self) -> int:
        return len(self.chunks)

    @property
    def all_scripts_ready(self) -> bool:
        """Đủ điều kiện để bấm "Chỉ tạo giọng đọc" — mọi chunk đã có kịch bản (không nhất thiết đã có giọng đọc)."""
        return bool(self.chunks) and all(c.has(Stage.SCRIPT) for c in self.chunks)

    @property
    def all_tts_ready(self) -> bool:
        """Đủ điều kiện để bấm "Ghép & xuất video" mà không cần tạo thêm giọng đọc."""
        return bool(self.chunks) and all(c.has(Stage.TTS) for c in self.chunks)

    def summary_line(self) -> str:
        if self.n_done == self.n_total:
            return f"Toàn bộ {self.n_total} chunk đã xử lý xong hoàn toàn — sẵn sàng có video."
        if self.n_done == 0:
            return f"Video dài {self.video_duration_label}, chia {self.n_total} chunk — chưa chunk nào xong hoàn toàn."
        return f"{self.n_done}/{self.n_total} chunk đã xong hoàn toàn; chunk #{self._first_unfinished + 1} đang dở ở bước '{self.chunks[self._first_unfinished].next_stage_label}'."

    @property
    def _first_unfinished(self) -> int:
        return next((c.index for c in self.chunks if not c.done), len(self.chunks) - 1)


def read_status(cfg: Config) -> PipelineStatus | None:
    """Trả về trạng thái hiện có, hoặc None nếu video này CHƯA từng được phân đoạn với đúng cấu hình hiện
    tại (chưa chạy lần nào, hoặc đã đổi cấu hình/video kể từ lần chạy trước — checkpoint theo fingerprint
    riêng cho từng tổ hợp, xem checkpoint.py)."""
    if not (cfg.video_path and cfg.video_path.is_file()):
        return None
    try:
        media = probe_media(cfg.video_path)
    except PipelineError:
        return None
    fp = build_fingerprint(cfg, media)
    checkpoint = PipelineCheckpoint(cfg.workdir, fp)
    plan = checkpoint.load_plan()
    if plan is None:
        return None
    chunks = []
    for c in plan:
        i = c["index"]
        stages_done = {s.value for s in ALL_STAGES if checkpoint.has(i, s)}
        chunks.append(ChunkStatus(index=i, start_sec=c["start_sec"], end_sec=c["end_sec"], stages_done=stages_done))
    return PipelineStatus(video_duration_label=format_timestamp(media.duration_sec), chunks=chunks)
