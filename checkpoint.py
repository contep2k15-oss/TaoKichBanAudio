"""Quản lý State/Checkpoint dạng JSON trung gian, cho phép RESUME khi pipeline bị gián đoạn giữa chừng
(rớt mạng, mất điện, Ctrl+C...) mà không phải chạy lại từ đầu.

Thiết kế:
  • Vì video dài được chia thành N macro-chunk độc lập (chunk_planner.py), checkpoint cũng theo TỪNG CHUNK:
    mỗi chunk đi qua 4 giai đoạn — 01_extracted → 02_script → 03_tts_done → 04_assembled — và MỖI giai đoạn
    của MỖI chunk là một file JSON riêng: `checkpoints/01_extracted_chunk0007.json`, v.v. Nhờ tách nhỏ,
    nếu rớt mạng ở chunk 12/20, 11 chunk trước đó (đã qua đủ 4 giai đoạn) được giữ nguyên, resume chỉ
    cần chạy tiếp từ chunk 12.
  • `checkpoints/00_chunks.json`: kế hoạch chia chunk (danh sách MacroChunk) — tính một lần bằng
    silence_detector + chunk_planner, lưu lại để lần resume sau không phải quét khoảng lặng lại từ đầu.
  • `checkpoints/<fingerprint-hash>/index.json`: mỗi tổ hợp "vân tay" (fingerprint — đường dẫn/kích thước/
    mtime video + các tham số cấu hình ảnh hưởng tới kết quả: ngôn ngữ, giọng, target_sec, model...) có một
    THƯ MỤC CON RIÊNG. Nhờ vậy đổi cấu hình/video KHÔNG BAO GIỜ xoá mất checkpoint của tổ hợp cũ — nó chỉ
    đơn giản là không được dùng lại (an toàn hơn dùng nhầm cache cũ), và nếu bạn đổi cấu hình về như trước,
    checkpoint tương ứng vẫn còn nguyên, resume lại được ngay.
"""
from __future__ import annotations

import hashlib
import json
import logging
from enum import Enum
from pathlib import Path
from typing import Any

from utils import PipelineError, read_json, write_json

log = logging.getLogger("svvc.checkpoint")


class Stage(str, Enum):
    EXTRACTED = "01_extracted"     # đã trích frame cho chunk này (danh sách đường dẫn ảnh + timestamp)
    SCRIPT = "02_script"           # đã có kịch bản (các đoạn thuyết minh) cho chunk này
    TTS = "03_tts_done"            # đã tổng hợp xong audio TTS + time-fit cho từng đoạn trong chunk
    ASSEMBLED = "04_assembled"     # đã ghép audio của chunk thành 1 file, sẵn sàng để mux cuối cùng

    @property
    def order(self) -> int:
        return list(Stage).index(self)


ALL_STAGES = list(Stage)


class PipelineCheckpoint:
    def __init__(self, workdir: Path, fingerprint: dict[str, Any], *, force: bool = False) -> None:
        self.fingerprint = fingerprint
        fp_hash = hashlib.sha1(json.dumps(fingerprint, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:12]
        self.dir = workdir / "checkpoints" / fp_hash
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self._completed: dict[str, set[str]] = {}   # chunk_key -> {stage.value đã xong}
        if force:
            self._reset_all()
        else:
            self._load_or_reset()

    # ── vòng đời ──────────────────────────────────────────
    def _load_or_reset(self) -> None:
        if not self.index_path.is_file():
            self._save_index()
            return
        try:
            data = read_json(self.index_path)
        except PipelineError as e:
            log.warning("index.json checkpoint hỏng (%s) → bắt đầu lại từ đầu.", e)
            self._reset_all()
            return
        self._completed = {k: set(v) for k, v in data.get("completed", {}).items()}
        n_chunks_done = sum(1 for stages in self._completed.values() if Stage.ASSEMBLED.value in stages)
        if n_chunks_done:
            log.info("Tìm thấy checkpoint hợp lệ (%s): %d chunk đã xử lý xong hoàn toàn → sẽ RESUME, bỏ qua các chunk đó.",
                     self.dir.name, n_chunks_done)

    def _reset_all(self) -> None:
        for f in self.dir.glob("*.json"):
            f.unlink(missing_ok=True)
        self._completed = {}
        self._save_index()

    def _save_index(self) -> None:
        write_json(self.index_path, {"fingerprint": self.fingerprint,
                                     "completed": {k: sorted(v) for k, v in self._completed.items()}})

    # ── truy vấn / ghi từng chunk-stage ─────────────────────
    @staticmethod
    def _key(chunk_id: int) -> str:
        return f"chunk{chunk_id:04d}"

    def stage_path(self, chunk_id: int, stage: Stage) -> Path:
        return self.dir / f"{stage.value}_{self._key(chunk_id)}.json"

    def has(self, chunk_id: int, stage: Stage) -> bool:
        return stage.value in self._completed.get(self._key(chunk_id), set()) and self.stage_path(chunk_id, stage).is_file()

    def load(self, chunk_id: int, stage: Stage) -> Any:
        if not self.has(chunk_id, stage):
            raise PipelineError(f"Checkpoint {stage.value} của chunk {chunk_id} chưa tồn tại.")
        return read_json(self.stage_path(chunk_id, stage))

    def save(self, chunk_id: int, stage: Stage, data: Any) -> None:
        write_json(self.stage_path(chunk_id, stage), data)
        self._completed.setdefault(self._key(chunk_id), set()).add(stage.value)
        self._save_index()
        log.debug("Checkpoint: chunk %d → %s đã lưu.", chunk_id, stage.value)

    def invalidate_from(self, chunk_id: int, stage: Stage) -> None:
        """Xoá checkpoint của `stage` trở đi cho 1 chunk (dùng khi muốn làm lại từ giữa, vd --force-chunk)."""
        key = self._key(chunk_id)
        for s in ALL_STAGES[stage.order:]:
            self.stage_path(chunk_id, s).unlink(missing_ok=True)
            self._completed.get(key, set()).discard(s.value)
        self._save_index()

    def chunk_done(self, chunk_id: int) -> bool:
        return self.has(chunk_id, Stage.ASSEMBLED)

    def next_stage(self, chunk_id: int) -> Stage | None:
        """Giai đoạn TIẾP THEO cần chạy cho chunk này, hoặc None nếu đã xong cả 4 giai đoạn."""
        for s in ALL_STAGES:
            if not self.has(chunk_id, s):
                return s
        return None

    # ── kế hoạch chia chunk (00_chunks.json) ────────────────
    @property
    def plan_path(self) -> Path:
        return self.dir / "00_chunks.json"

    def load_plan(self) -> list[dict] | None:
        if not self.plan_path.is_file():
            return None
        try:
            data = read_json(self.plan_path)
        except PipelineError:
            return None
        return data.get("chunks") if data.get("fingerprint") == self.fingerprint else None

    def save_plan(self, chunks: list[dict]) -> None:
        write_json(self.plan_path, {"fingerprint": self.fingerprint, "chunks": chunks})

    # ── tóm tắt cho log / CLI ────────────────────────────────
    def resume_summary(self, total_chunks: int) -> str:
        done = sum(1 for i in range(total_chunks) if self.chunk_done(i))
        if done == 0:
            return f"Bắt đầu từ đầu ({total_chunks} chunk)."
        if done == total_chunks:
            return f"Toàn bộ {total_chunks} chunk đã xử lý xong ở lần chạy trước."
        return f"Resume: {done}/{total_chunks} chunk đã xong, tiếp tục từ chunk #{done}."
