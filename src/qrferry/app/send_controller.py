"""发送控制器 —— 封装发送端 core 流水线，按协议 §7.1 生成循环帧流。

职责：压缩 → 分块 → LT 编码 → 封帧，产出 `MANIFEST → DATA× → END` 的循环帧流，
供 UI 层按帧率取帧、渲染 QR（单码或网格）。与 Qt 解耦，纯逻辑可单测。
"""
from __future__ import annotations

import base64
import hashlib
import zlib
from dataclasses import dataclass, replace
from typing import Iterator

from qrferry.core import chunker, lt
from qrferry.core.frame import (
    Compression, DataPayload, EndPayload, FrameHeader, FrameType,
    LtDistribution, ManifestPayload, encode_frame,
)

__all__ = [
    "SenderConfig", "SendController", "recommend_chunk_size_log",
    "COLOR_MATRIX_CHUNK_SIZE_LOG", "COLOR_MATRIX_MAX_FRAME_BYTES",
]


DEFAULT_CHUNK_SIZE_LOG = 7
LARGE_GRID_CHUNK_SIZE_LOG = 8
LARGE_SINGLE_CHUNK_SIZE_LOG = 9
COLOR_MATRIX_CHUNK_SIZE_LOG = 9
COLOR_MATRIX_MAX_FRAME_BYTES = 720
DEFAULT_MANIFEST_INTERVAL = 32


def recommend_chunk_size_log(encoded_size: int, grid: tuple[int, int] = (1, 1)) -> int:
    """按压缩后负载与显示形态选择源块大小。

    目标是控制视觉密度：多码网格优先保持低 version；单码大文件则用更大的源块降低 K。
    """
    cells = max(1, grid[0] * grid[1])
    if cells >= 9:
        return LARGE_GRID_CHUNK_SIZE_LOG if encoded_size >= 128 * 1024 else DEFAULT_CHUNK_SIZE_LOG
    if cells > 1:
        if encoded_size >= 512 * 1024:
            return LARGE_SINGLE_CHUNK_SIZE_LOG
        return LARGE_GRID_CHUNK_SIZE_LOG if encoded_size >= 128 * 1024 else DEFAULT_CHUNK_SIZE_LOG
    if encoded_size >= 512 * 1024:
        return LARGE_SINGLE_CHUNK_SIZE_LOG
    if encoded_size >= 128 * 1024:
        return 8
    return DEFAULT_CHUNK_SIZE_LOG


@dataclass
class SenderConfig:
    chunk_size_log: int | None = None   # None=按负载自动选择；显式值用于测试/协议 fixtures
    compression: int | None = None       # None=仅在压缩后更小时使用 zlib
    ecc_level: str = "M"
    redundancy: float = 2.0
    lt_dist: int = int(LtDistribution.RSD)
    grid: tuple[int, int] = (1, 1)   # (rows, cols)
    rounds: int = 3                  # 兜底播放轮数（协议 §12）
    max_frame_bytes: int = lt.SAFE_FRAME_BYTES
    manifest_interval: int = 0       # >0 时每 N 个 DATA 额外插入一次 MANIFEST，避免接收端中途启动错过会话头


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

        compression = self.config.compression
        if compression is None:
            compressed = zlib.compress(data, 9)
            if len(compressed) < len(data):
                compression = int(Compression.ZLIB)
                self._encoded = compressed
            else:
                compression = int(Compression.NONE)
                self._encoded = data
            self.config = replace(self.config, compression=compression)
        elif compression == int(Compression.NONE):
            self._encoded = data
        elif compression == int(Compression.ZLIB):
            self._encoded = zlib.compress(data, 9)
        else:
            raise ValueError(f"暂不支持的压缩类型: {compression}")
        chunk_size_log = self.config.chunk_size_log
        if chunk_size_log is None:
            chunk_size_log = recommend_chunk_size_log(len(self._encoded), self.config.grid)
            self.config = replace(self.config, chunk_size_log=chunk_size_log)
        if not 1 <= chunk_size_log <= 20:
            raise ValueError("chunk_size_log 必须在 1..20 之间")

        self._blocks = chunker.split(self._encoded, 1 << chunk_size_log)
        self.K = len(self._blocks)
        self._sha = hashlib.sha256(data).digest()
        self._encoder = lt.LtEncoder(
            self._blocks, session_id, dist=self.config.lt_dist,
            max_frame_bytes=self.config.max_frame_bytes)

        self._manifest_frame = encode_frame(
            FrameHeader(FrameType.MANIFEST, session_id=session_id),
            ManifestPayload(content_type, self.config.compression, chunk_size_log,
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
        """总帧数估算：每轮 MANIFEST/DATA，完整流末尾只有一个 END。"""
        extra_manifest = (
            (self._round_symbols - 1) // self.config.manifest_interval
            if self.config.manifest_interval > 0 else 0
        )
        return (1 + self._round_symbols + extra_manifest) * self.config.rounds + 1

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
        """循环帧流：每轮 MANIFEST → DATA×round_symbols，最后 END。

        基于实例游标 self._next_sid 推进；多次迭代从当前游标继续而非重置，
        因此「播完主轮 → 调 extra_data_frames 补发」可无缝衔接。
        """
        for _ in range(self.config.rounds):
            yield self._manifest_frame
            for i in range(self._round_symbols):
                if self.config.manifest_interval > 0 and i > 0 and i % self.config.manifest_interval == 0:
                    yield self._manifest_frame
                yield self.next_data_frame()
        yield self._end_frame

    def infinite_frames(self) -> Iterator[bytes]:
        """无限发送流：周期性 MANIFEST + 不断追加新的 DATA 符号。

        光学单向链路无法保证接收端启动时机；该流适合 UI 持续发送，避免播完有限轮后停在 END。
        """
        data_since_manifest = 0
        while True:
            if data_since_manifest == 0:
                yield self._manifest_frame
            yield self.next_data_frame()
            data_since_manifest += 1
            if self.config.manifest_interval > 0 and data_since_manifest >= self.config.manifest_interval:
                data_since_manifest = 0

    def rolling_frames(self, manifest_interval: int | None = None) -> Iterator[bytes]:
        """顺序循环发送源块；丢失分片在下一轮自动补齐。"""
        interval = manifest_interval or self.config.manifest_interval or DEFAULT_MANIFEST_INTERVAL
        if interval <= 0:
            raise ValueError("manifest_interval 必须为正数")
        data_since_manifest = interval
        block_index = 0
        while True:
            if data_since_manifest >= interval:
                data_since_manifest = 0
                yield self._manifest_frame
            payload = DataPayload(1, [block_index], self._blocks[block_index]).pack()
            yield encode_frame(
                FrameHeader(
                    FrameType.DATA,
                    session_id=self.session_id,
                    symbol_id=self._next_sid,
                ),
                payload,
            )
            self._next_sid += 1
            block_index = (block_index + 1) % self.K
            data_since_manifest += 1

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
                "max_frame_bytes": self.config.max_frame_bytes,
                "manifest_interval": self.config.manifest_interval,
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
            max_frame_bytes=cfg_d.get("max_frame_bytes", lt.SAFE_FRAME_BYTES),
            manifest_interval=cfg_d.get("manifest_interval", 0),
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
