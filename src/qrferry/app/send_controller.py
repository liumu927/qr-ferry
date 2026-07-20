"""发送控制器 —— 封装发送端 core 流水线，按协议 §7.1 生成循环帧流。

职责：压缩 → 分块 → LT 编码 → 封帧，产出 `MANIFEST → DATA× → END` 的循环帧流，
供 UI 层按帧率取帧、渲染 QR（单码或网格）。与 Qt 解耦，纯逻辑可单测。
"""
from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass
from typing import Iterator

from qrferry.core import chunker, lt
from qrferry.core.frame import (
    Compression, DataPayload, EndPayload, FrameHeader, FrameType,
    LtDistribution, ManifestPayload, encode_frame,
)

__all__ = ["SenderConfig", "SendController"]


@dataclass
class SenderConfig:
    chunk_size_log: int = 7   # 128B；让 DATA 帧 QR 保持低 version（v14-17），摄像头可识别
    compression: int = int(Compression.ZLIB)
    ecc_level: str = "Q"
    redundancy: float = 2.0
    lt_dist: int = int(LtDistribution.RSD)
    grid: tuple[int, int] = (1, 1)   # (rows, cols)
    rounds: int = 3                  # 兜底播放轮数（协议 §12）


class SendController:
    """发送端逻辑：预处理一次，迭代产出循环帧流。"""

    def __init__(self, data: bytes, content_type: int, filename: str,
                 session_id: int, config: SenderConfig | None = None):
        if not data:
            raise ValueError("发送数据不能为空")
        self.config = config or SenderConfig()
        self.session_id = session_id
        self.content_type = content_type
        self.filename = filename
        self.raw_size = len(data)

        if self.config.compression == int(Compression.NONE):
            self._encoded = data
        elif self.config.compression == int(Compression.ZLIB):
            self._encoded = zlib.compress(data, 6)
        else:
            raise ValueError(f"暂不支持的压缩类型: {self.config.compression}")
        self._blocks = chunker.split(self._encoded, 1 << self.config.chunk_size_log)
        self.K = len(self._blocks)
        self._sha = hashlib.sha256(data).digest()
        self._encoder = lt.LtEncoder(self._blocks, session_id, dist=self.config.lt_dist)

        self._manifest_frame = encode_frame(
            FrameHeader(FrameType.MANIFEST, session_id=session_id),
            ManifestPayload(content_type, self.config.compression, self.config.chunk_size_log,
                            self.config.lt_dist, self.K, self.raw_size, len(self._encoded),
                            self._sha, filename).pack())
        self._end_frame = encode_frame(
            FrameHeader(FrameType.END, session_id=session_id), EndPayload(self._sha).pack())
        self._round_symbols = max(1, int(self.K * self.config.redundancy))

    @property
    def grid_cells(self) -> int:
        return self.config.grid[0] * self.config.grid[1]

    @property
    def estimated_frames(self) -> int:
        """总帧数估算：(MANIFEST + DATA×round_symbols + END) × rounds。"""
        return (1 + self._round_symbols + 1) * self.config.rounds

    def __iter__(self) -> Iterator[bytes]:
        """循环帧流：每轮 MANIFEST → DATA×round_symbols → END，symbol_id 全局递增。"""
        sid = 0
        for _ in range(self.config.rounds):
            yield self._manifest_frame
            for _ in range(self._round_symbols):
                degree, adj, xd = self._encoder.encode_symbol(sid)
                payload = DataPayload(degree, adj, xd).pack()
                yield encode_frame(
                    FrameHeader(FrameType.DATA, session_id=self.session_id, symbol_id=sid),
                    payload)
                sid += 1
            yield self._end_frame
