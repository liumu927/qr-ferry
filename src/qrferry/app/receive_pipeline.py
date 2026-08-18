"""接收流水线 —— 图像 → 解码 → 会话 → 校验 的纯逻辑层。

与 Qt/摄像头解耦：上层（UI/控制器）负责采集帧图像传入，本模块负责
解码多码 → CRC 校验 → 驱动 ReceiveSession → 完成后解压 + SHA-256 校验，
文件数据由上层在用户确认保存后落盘。
"""
from __future__ import annotations

import hashlib
import math
import os
import time
import zlib
from collections import deque
from dataclasses import dataclass

from qrferry.app import session_store
from qrferry.core.frame import (
    Compression,
    ContentType,
    FrameType,
    ProtocolError,
    decode_frame,
)
from qrferry.core.lt import expected_overhead
from qrferry.core.session import ReceiveSession
from qrferry.qr.backend import CodecBackend, StandardQrBackend

__all__ = ["ReceivePipeline", "ReceiveResult", "safe_filename"]


@dataclass
class ReceiveResult:
    content_type: int
    filename: str
    data: bytes


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
        self.valid_data_frames = 0     # 有效 DATA 帧数（不含 MANIFEST/END）
        self.total_valid_bytes = 0     # 累计有效 DATA 帧承载字节（链路搬运量，速率基数）
        self._samples: deque = deque()  # (monotonic_ts, bytes) 滑动窗口；工作线程 append、UI 线程 popleft
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
        return max(0.0, time.monotonic() - self.first_valid_frame_ts)

    @property
    def link_bps_avg(self) -> float:
        """累计平均链路速率（字节/秒）：总有效字节 / 耗时。"""
        e = self.elapsed_seconds
        return self.total_valid_bytes / e if e > 0 else 0.0

    @property
    def effective_bps(self) -> float:
        """有效速率（去重，字节/秒）：唯一符号 × 块大小 / 耗时。

        与 link_bps（含重复扫到的同块 + 喷泉冗余符号）不同，只计去重后真正推进
        进度的字节，故与 progress/ETA/完成时间自洽——这才是用户视角的「文件搬运速度」。
        链路速率反映扫码吞吐（识别效率×帧率），有效速率反映传输进度，二者后期差距拉大。
        """
        m = self.session.manifest
        unique = self.session.unique_symbols
        e = self.elapsed_seconds
        if m is None or unique <= 0 or e <= 0:
            return 0.0
        return unique * (1 << m.chunk_size_log) / e

    @property
    def fps_avg(self) -> float:
        """累计平均有效 DATA 帧率。"""
        e = self.elapsed_seconds
        return self.valid_data_frames / e if e > 0 else 0.0

    def _instant_window(self, window: float = 2.0) -> tuple[float, float]:
        """近 window 秒滑动窗口的 (字节速率, 帧速率)。UI 线程调用：

        清理过期样本（deque 支持工作线程 append、本线程 popleft 的并发），再求和。
        样本不足时返回 (0.0, 0.0)，由调用方回退到均值。
        """
        now = time.monotonic()
        cutoff = now - window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if not self._samples:
            return 0.0, 0.0
        oldest = self._samples[0][0]
        span = max(1e-3, now - oldest)
        total = sum(bs for _, bs in self._samples)
        return total / span, len(self._samples) / span

    @property
    def link_bps_instant(self) -> float:
        """瞬时链路速率（近 ~2s 滑动窗口）；样本不足时回退均值。"""
        if len(self._samples) < 2:
            return self.link_bps_avg
        bps, _ = self._instant_window()
        return bps

    @property
    def fps_instant(self) -> float:
        """瞬时有效帧率；样本不足时回退均值。"""
        if len(self._samples) < 2:
            return self.fps_avg
        _, fps = self._instant_window()
        return fps

    @property
    def eta_seconds(self) -> float | None:
        """预计剩余秒数：基于喷泉开销模型与唯一符号到达速率。

        门槛——唯一符号 <3 或耗时 <1s 时返回 None（速率未稳定，估值无意义）。
        overshoot（实际超出预期帧数）时按 decimen progress.ts 步进上修 target，
        诚实反映「比名义开销更慢」，避免 ETA 装作精确。
        """
        unique = self.session.unique_symbols
        elapsed = self.elapsed_seconds
        if unique < 3 or elapsed < 1.0:
            return None
        K = self.session.K
        if K <= 0:
            return None
        expected = max(K + 1, math.ceil(K * expected_overhead(K)))
        redundancy = max(1, expected - K)
        overshoot = unique - expected
        target = (expected if overshoot < 0
                  else expected + redundancy * (overshoot // redundancy + 1))
        rate = unique / elapsed
        if rate <= 0:
            return None
        return (target - unique) / rate

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
            effective = self.session.ingest(header, payload)
            # 链路统计（link_bps 样本）：CRC 通过且会话已建立的 DATA 帧都算搬运量，
            # 含重复符号（重复也消耗带宽）——与 effective 无关，保持原语义。
            if header.frame_type == FrameType.DATA and self.session.manifest is not None:
                bs = 1 << self.session.manifest.chunk_size_log
                self.total_valid_bytes += bs
                self.valid_data_frames += 1
                self._samples.append((time.monotonic(), bs))
            if not effective:
                continue   # 未生效帧（MANIFEST 前的 DATA/重复符号/未知会话）：不计入会话统计
            added += 1
        self.valid_frames += added
        if added > 0 and self.first_valid_frame_ts is None:
            self.first_valid_frame_ts = time.monotonic()
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
        self.result = ReceiveResult(
            m.content_type,
            m.filename if m.content_type == ContentType.FILE else "",
            data,
        )
        self._finalized = True
        if self._persist:
            session_store.clear(self.save_dir)   # 传输完成，清理续传快照
