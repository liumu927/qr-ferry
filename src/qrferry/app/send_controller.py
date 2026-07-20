"""发送控制器 —— 封装发送端 core 流水线，按协议 §7.1 生成循环帧流。

职责：压缩 → 分块 → LT 编码 → 封帧，产出 `MANIFEST → DATA× → END` 的循环帧流，
供 UI 层按帧率取帧、渲染 QR（单码或网格）。与 Qt 解耦，纯逻辑可单测。
"""
from __future__ import annotations

import base64
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
                 session_id: int, config: SenderConfig | None = None,
                 source_kind: str | None = None, source_path: str | None = None):
        if not data:
            raise ValueError("发送数据不能为空")
        self.config = config or SenderConfig()
        self.session_id = session_id
        self.content_type = content_type
        self.filename = filename
        self.raw_size = len(data)
        self._raw_data = data             # 原始数据引用，供 TEXT 模式续传持久化
        self._source_kind = source_kind   # "file" | "text" | None
        self._source_path = source_path   # FILE: 原文件路径；TEXT: 由 store 落 bin 后回填

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
        self._next_sid = 0   # 全局符号游标；正常播放/人工补发/断点续传共用，确保 symbol_id 单调递增

    @property
    def grid_cells(self) -> int:
        return self.config.grid[0] * self.config.grid[1]

    @property
    def estimated_frames(self) -> int:
        """总帧数估算：(MANIFEST + DATA×round_symbols + END) × rounds。"""
        return (1 + self._round_symbols + 1) * self.config.rounds

    @property
    def next_sid(self) -> int:
        """当前符号游标（下一个将发的 symbol_id）。供断点续传快照使用。"""
        return self._next_sid

    def next_data_frame(self) -> bytes:
        """生成下一个 DATA 帧（symbol_id=_next_sid），游标 +1。

        LT 喷泉码对 (session_id, symbol_id) 确定性映射，故游标单调递增即可保证
        每次产出不重复的新编码符号——这是人工补发能补缺块的根基（重发旧 sid 无增益）。
        """
        degree, adj, xd = self._encoder.encode_symbol(self._next_sid)
        payload = DataPayload(degree, adj, xd).pack()
        frame = encode_frame(
            FrameHeader(FrameType.DATA, session_id=self.session_id, symbol_id=self._next_sid),
            payload)
        self._next_sid += 1
        return frame

    def extra_data_frames(self, n: int) -> Iterator[bytes]:
        """追加产出 n 个新 DATA 帧（新 symbol_id），供人工补发使用。"""
        if n < 0:
            raise ValueError("补发帧数不能为负")
        for _ in range(n):
            yield self.next_data_frame()

    def __iter__(self) -> Iterator[bytes]:
        """循环帧流：每轮 MANIFEST → DATA×round_symbols → END。

        基于实例游标 self._next_sid 推进；多次迭代从当前游标继续而非重置，
        因此「播完主轮 → 调 extra_data_frames 补发」可无缝衔接。
        """
        for _ in range(self.config.rounds):
            yield self._manifest_frame
            for _ in range(self._round_symbols):
                yield self.next_data_frame()
            yield self._end_frame

    @property
    def raw_data(self) -> bytes:
        """原始发送数据（供 TEXT 模式断点续传持久化）。"""
        return self._raw_data

    # ── 断点续传 ──
    def to_snapshot(self) -> dict:
        """导出可 JSON 序列化的发送端快照。"""
        return {
            "session_id": self.session_id,
            "content_type": self.content_type,
            "filename": self.filename,
            "raw_size": self.raw_size,
            "config": {
                "chunk_size_log": self.config.chunk_size_log,
                "compression": self.config.compression,
                "ecc_level": self.config.ecc_level,
                "redundancy": self.config.redundancy,
                "lt_dist": self.config.lt_dist,
                "grid": list(self.config.grid),
                "rounds": self.config.rounds,
            },
            "next_sid": self._next_sid,
            "source_kind": self._source_kind,
            "source_path": self._source_path,
            "raw_sha_b64": base64.b64encode(self._sha).decode("ascii"),
        }

    @classmethod
    def from_snapshot(cls, snap: dict) -> "SendController":
        """从快照重建发送端：FILE 模式重读文件并校验 raw_sha，TEXT 模式从 bin 还原。

        确定性根基：相同 (session_id, blocks) → encode_symbol(sid) 可复现，故只需把
        _next_sid 设为快照值，重建的 encoder 即从该游标续传相同符号。
        """
        cfg_d = snap["config"]
        cfg = SenderConfig(
            chunk_size_log=cfg_d["chunk_size_log"],
            compression=cfg_d["compression"],
            ecc_level=cfg_d["ecc_level"],
            redundancy=cfg_d["redundancy"],
            lt_dist=cfg_d["lt_dist"],
            grid=tuple(cfg_d["grid"]),
            rounds=cfg_d["rounds"],
        )
        kind = snap.get("source_kind")
        path = snap.get("source_path")
        if not kind or not path:
            raise ValueError("快照缺少数据来源")
        with open(path, "rb") as f:
            data = f.read()
        if kind == "file":
            if hashlib.sha256(data).digest() != base64.b64decode(snap["raw_sha_b64"]):
                raise ValueError(f"文件 {path} 自上次发送后已变更，无法续传")
        ctrl = cls(data, snap["content_type"], snap["filename"], snap["session_id"], cfg,
                   source_kind=kind, source_path=path)
        ctrl._next_sid = snap["next_sid"]
        return ctrl
