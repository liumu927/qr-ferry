"""接收流水线 —— 图像 → 解码 → 会话 → 落盘 的纯逻辑层。

与 Qt/摄像头解耦：上层（UI/控制器）负责采集帧图像传入，本模块负责
解码多码 → CRC 校验 → 驱动 ReceiveSession → 完成后解压 + SHA-256 校验 + 落盘/文本。
"""
from __future__ import annotations

import hashlib
import os
import time
import zlib
from dataclasses import dataclass

from qrferry.core.frame import Compression, ContentType, ProtocolError, decode_frame
from qrferry.core.session import ReceiveSession
from qrferry.qr.backend import CodecBackend, StandardQrBackend
from qrferry.app import session_store

__all__ = ["ReceiveResult", "ReceivePipeline", "safe_filename"]


@dataclass
class ReceiveResult:
    content_type: int
    filename: str
    data: bytes
    path: str | None       # 文件落盘绝对路径；文本传输为 None


def safe_filename(name: str) -> str:
    """剥离路径穿越与非法字符（协议 §11 安全模型）。"""
    base = os.path.basename(name.replace("\\", "/")).strip()
    if base in ("", ".", ".."):
        base = "received"
    for ch in '<>:"/\\|?*':
        base = base.replace(ch, "_")
    return base[:200]


class ReceivePipeline:
    """接收端核心：喂入图像，自动解码并累积，完成后产出结果。"""

    def __init__(self, session: ReceiveSession | None = None,
                 backend: CodecBackend | None = None, save_dir: str = ".",
                 resume_from: ReceiveSession | None = None, persist: bool = True):
        self.session = session or resume_from or ReceiveSession()
        self.backend = backend or StandardQrBackend()
        self.save_dir = save_dir
        self._finalized = False
        self.result: ReceiveResult | None = None
        self._persist = persist
        self._ingested_since_save = 0
        self.first_valid_frame_ts: float | None = None
        self.valid_frames = 0          # 有效帧数（CRC 通过并入会话）
        self.bad_frames = 0            # 丢弃帧数（CRC 失败/解析失败）
        self.missed_images = 0         # 物理码未解出的图像帧数
        if persist and resume_from is not None and resume_from.to_snapshot() is not None:
            session_store.save(self.session, save_dir)   # 恢复后立即落盘，固化种子

    @property
    def progress(self) -> float:
        return self.session.progress

    @property
    def missing_indices(self) -> list[int]:
        return self.session.missing_indices

    @property
    def drop_rate(self) -> float:
        """丢弃率 = bad_frames / (valid+bad)，链路质量指标。"""
        total = self.valid_frames + self.bad_frames
        return self.bad_frames / total if total else 0.0

    @property
    def elapsed_seconds(self) -> float:
        if self.first_valid_frame_ts is None:
            return 0.0
        return max(0.0, time.time() - self.first_valid_frame_ts)

    @property
    def is_complete(self) -> bool:
        return self.session.is_complete

    def process_image(self, image) -> int:
        """解码图像中所有 QR，有效帧（CRC 通过）入会话；返回本帧有效帧数。"""
        added = 0
        decoded = self.backend.decode(image)
        if not decoded:
            self.missed_images += 1
        for raw in decoded:
            try:
                header, payload = decode_frame(raw)
            except ProtocolError:
                self.bad_frames += 1   # CRC 失败/坏帧：丢弃但计数
                continue
            self.session.ingest(header, payload)
            added += 1
        self.valid_frames += added
        if added > 0 and self.first_valid_frame_ts is None:
            self.first_valid_frame_ts = time.time()
        if self.session.is_complete and not self._finalized:
            self._finalize()
        elif self._persist and added > 0:
            self._maybe_save(added)
        return added

    def _maybe_save(self, added: int) -> None:
        """节流持久化：每累计 16 个有效帧存一次，避免每帧 IO。"""
        self._ingested_since_save += added
        if self._ingested_since_save >= 16:
            self._ingested_since_save = 0
            session_store.save(self.session, self.save_dir)

    def _finalize(self) -> None:
        m = self.session.manifest
        encoded = self.session.reassemble()
        if m.compression == Compression.NONE:
            data = encoded
        elif m.compression == Compression.ZLIB:
            data = zlib.decompress(encoded)
        else:
            raise ValueError(f"暂不支持的压缩类型: {m.compression}")
        if hashlib.sha256(data).digest() != m.raw_sha256:
            raise ValueError("SHA-256 校验失败：数据损坏")
        if m.content_type == ContentType.TEXT:
            self.result = ReceiveResult(m.content_type, "", data, None)
        else:
            os.makedirs(self.save_dir, exist_ok=True)
            path = os.path.join(self.save_dir, safe_filename(m.filename))
            with open(path, "wb") as f:
                f.write(data)
            self.result = ReceiveResult(m.content_type, m.filename, data, path)
        self._finalized = True
        if self._persist:
            session_store.clear(self.save_dir)   # 传输完成，清理续传快照
