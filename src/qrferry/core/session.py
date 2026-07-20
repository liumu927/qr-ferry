"""接收会话状态机 —— 协议 v1.0 §7。

管理单个 SESSION_ID 的接收上下文：
- 首次 MANIFEST 建立上下文（LtDecoder + 槽位），重复 MANIFEST 幂等忽略；
- DATA 符号累积送入 LtDecoder；
- END 触发收尾（不强制，is_complete 由 decoder 判定）。

输出重组后的压缩字节流；解压与 SHA-256 校验交给上层，保持本模块职责单一。
坏 payload 静默丢弃（§11 安全模型：解析失败绝不崩溃）。
"""
from __future__ import annotations

from qrferry.core import chunker, lt
from qrferry.core.frame import (
    Compression, ContentType, DataPayload, EndPayload, FrameHeader, FrameType,
    LtDistribution, ManifestPayload, ProtocolError,
)

__all__ = ["ReceiveSession"]


class ReceiveSession:
    def __init__(self) -> None:
        self.session_id: int | None = None
        self.manifest: ManifestPayload | None = None
        self._decoder: lt.LtDecoder | None = None
        self._ended: bool = False

    # ── 状态查询 ──
    @property
    def started(self) -> bool:
        return self.manifest is not None

    @property
    def K(self) -> int:
        return self.manifest.total_chunks if self.manifest else 0

    @property
    def is_complete(self) -> bool:
        return self._decoder is not None and self._decoder.is_complete

    @property
    def progress(self) -> float:
        """已恢复源块占比，[0.0, 1.0]。"""
        if self._decoder is None or self.K == 0:
            return 0.0
        return self._decoder.resolved_count / self.K

    # ── 帧路由 ──
    def ingest(self, header: FrameHeader, payload: bytes) -> None:
        """按 FRAME_TYPE 路由；未知会话或坏 payload 一律静默忽略。"""
        try:
            if header.frame_type == FrameType.MANIFEST:
                self._on_manifest(header, payload)
            elif header.frame_type == FrameType.DATA:
                self._on_data(header, payload)
            elif header.frame_type == FrameType.END:
                self._on_end(header, payload)
        except ProtocolError:
            return   # 坏 payload：丢弃，不崩溃

    def _on_manifest(self, header: FrameHeader, payload: bytes) -> None:
        m = ManifestPayload.unpack(payload)
        if m.content_type not in (ContentType.FILE, ContentType.TEXT):
            raise ProtocolError("未知 content_type")
        if m.compression not in (Compression.NONE, Compression.ZLIB):
            raise ProtocolError("未知或暂不支持 compression")
        if m.lt_dist not in (LtDistribution.RSD, LtDistribution.ISD, LtDistribution.DEGENERATE):
            raise ProtocolError("未知 lt_dist")
        if m.total_chunks <= 0:
            raise ProtocolError("total_chunks 必须为正")
        if not 1 <= m.chunk_size_log <= 20:
            raise ProtocolError("chunk_size_log 越界")
        block_size = 1 << m.chunk_size_log
        if m.encoded_size > m.total_chunks * block_size:
            raise ProtocolError("encoded_size 超出分块容量")
        if self.started and self.session_id == header.session_id:
            return   # 同会话重播，幂等忽略
        # 首次或切换会话：重建上下文
        self.session_id = header.session_id
        self.manifest = m
        self._decoder = lt.LtDecoder(m.total_chunks, block_size)
        self._ended = False

    def _on_data(self, header: FrameHeader, payload: bytes) -> None:
        if self._decoder is None or self.manifest is None or header.session_id != self.session_id:
            return   # 未知会话的 DATA：忽略（MANIFEST 未到）
        d = DataPayload.unpack(payload)
        block_size = 1 << self.manifest.chunk_size_log
        if d.degree <= 0 or len(d.xor_data) != block_size:
            raise ProtocolError("DATA 符号尺寸非法")
        if tuple(sorted(set(d.adjacency))) != d.adjacency:
            raise ProtocolError("DATA adjacency 必须升序且去重")
        if any(i < 0 or i >= self.K for i in d.adjacency):
            raise ProtocolError("DATA adjacency 越界")
        self._decoder.add_symbol(d.adjacency, d.xor_data)

    def _on_end(self, header: FrameHeader, payload: bytes) -> None:
        if self._decoder is None or header.session_id != self.session_id:
            return
        EndPayload.unpack(payload)   # 仅校验合法性
        self._ended = True

    # ── 重组 ──
    def reassemble(self) -> bytes:
        """返回重组后的压缩字节流（调用前需 is_complete）。"""
        if not self.is_complete or self.manifest is None or self._decoder is None:
            raise RuntimeError("会话未完成，无法重组")
        blocks = self._decoder.get_blocks()
        return chunker.join(blocks, total_size=self.manifest.encoded_size)
