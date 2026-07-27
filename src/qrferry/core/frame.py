"""帧编解码 —— 协议 v1.0 §4/§5。

帧 = 18B Header + 变长 Payload + 4B CRC32（小端）。
所有解析/校验失败统一抛 ProtocolError；接收端捕获后丢弃坏帧，绝不崩溃。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from qrferry.core import crc32

__all__ = [
    "CRC_SIZE",
    "HEADER_SIZE",
    "MAGIC",
    "VERSION",
    "Compression",
    "ContentType",
    "DataPayload",
    "EndPayload",
    "FrameHeader",
    "FrameType",
    "LtDistribution",
    "ManifestPayload",
    "ProtocolError",
    "decode_frame",
    "encode_frame",
]

# ── 协议常量 ──────────────────────────────────────────────
MAGIC = 0x4F51            # 'O','Q' —— 帧同步标识，用于过滤摄像头误识别
VERSION = 0x01            # 协议版本 v1.0
HEADER_SIZE = 18
CRC_SIZE = 4
_SHA256_SIZE = 32


class FrameType(IntEnum):
    MANIFEST = 0x01
    DATA = 0x02
    END = 0x03
    FEEDBACK = 0x04   # 预留：未来双向光学回传
    CIMBAR = 0x05     # 预留：彩色矩阵码模式


class ContentType(IntEnum):
    FILE = 0x01
    TEXT = 0x02


class Compression(IntEnum):
    NONE = 0x00
    ZLIB = 0x01
    ZSTD = 0x02   # 预留


class LtDistribution(IntEnum):
    RSD = 0x00        # 鲁棒孤子分布（默认）
    ISD = 0x01        # 理想孤子分布
    DEGENERATE = 0x02 # 仅链路调试


class ProtocolError(ValueError):
    """帧/载荷解析或 CRC 校验失败。接收端捕获后丢弃坏帧。"""


# ── Header (§4) ───────────────────────────────────────────
# MAGIC(u16) VERSION(u8) TYPE(u8) SESSION(u32) STREAM(u32) SYMBOL(u32) PAYLOAD_LEN(u16)
_HEADER_FMT = "<HBBIIIH"


@dataclass
class FrameHeader:
    frame_type: int
    session_id: int
    stream_id: int = 0
    symbol_id: int = 0
    version: int = VERSION
    payload_len: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            _HEADER_FMT, MAGIC, self.version, self.frame_type,
            self.session_id, self.stream_id, self.symbol_id, self.payload_len,
        )

    @staticmethod
    def unpack(buf: bytes) -> FrameHeader:
        if len(buf) < HEADER_SIZE:
            raise ProtocolError("header 长度不足")
        magic, version, ftype, sid, stream, sym, plen = struct.unpack_from(_HEADER_FMT, buf, 0)
        if magic != MAGIC:
            raise ProtocolError(f"bad MAGIC: {magic:#06x}")
        return FrameHeader(
            frame_type=ftype, session_id=sid, stream_id=stream,
            symbol_id=sym, version=version, payload_len=plen,
        )


# ── MANIFEST Payload (§5.2) ──────────────────────────────
@dataclass
class ManifestPayload:
    content_type: int
    compression: int
    chunk_size_log: int
    lt_dist: int
    total_chunks: int
    raw_size: int
    encoded_size: int
    raw_sha256: bytes
    filename: str = ""

    MAX_FILENAME = 255

    def pack(self) -> bytes:
        name = self.filename.encode("utf-8")
        if len(name) > self.MAX_FILENAME:
            raise ProtocolError(f"filename 过长: {len(name)}>{self.MAX_FILENAME}")
        if len(self.raw_sha256) != _SHA256_SIZE:
            raise ProtocolError("raw_sha256 必须 32 字节")
        out = struct.pack(
            "<BBBBI", self.content_type, self.compression,
            self.chunk_size_log, self.lt_dist, self.total_chunks,
        )
        out += struct.pack("<QQ", self.raw_size, self.encoded_size)
        out += struct.pack("<B", len(name))
        out += name
        out += self.raw_sha256
        return out

    @staticmethod
    def unpack(buf: bytes) -> ManifestPayload:
        # 固定前缀 = <BBBBI>(8) + <QQ>(16) + <B>(1) = 25
        if len(buf) < 25 + _SHA256_SIZE:
            raise ProtocolError("manifest payload 过短")
        ctype, comp, csl, ltd, total = struct.unpack_from("<BBBBI", buf, 0)
        raw_size, enc_size = struct.unpack_from("<QQ", buf, 8)
        (name_len,) = struct.unpack_from("<B", buf, 24)
        name_end = 25 + name_len
        if len(buf) < name_end + _SHA256_SIZE:
            raise ProtocolError("manifest filename/sha256 越界")
        filename = buf[25:name_end].decode("utf-8", errors="replace")
        sha = bytes(buf[name_end:name_end + _SHA256_SIZE])
        return ManifestPayload(
            ctype, comp, csl, ltd, total, raw_size, enc_size, sha, filename,
        )


# ── DATA Payload (§5.3 LT 编码符号) ──────────────────────
@dataclass
class DataPayload:
    degree: int
    adjacency: tuple[int, ...]   # 升序、去重的源块索引列表
    xor_data: bytes

    def pack(self) -> bytes:
        if len(self.adjacency) != self.degree:
            raise ProtocolError("adjacency 长度与 degree 不符")
        out = struct.pack(f"<H{self.degree}I", self.degree, *self.adjacency)
        return out + self.xor_data

    @staticmethod
    def unpack(buf: bytes) -> DataPayload:
        if len(buf) < 2:
            raise ProtocolError("data payload 过短")
        (degree,) = struct.unpack_from("<H", buf, 0)
        adj_end = 2 + degree * 4
        if len(buf) < adj_end:
            raise ProtocolError("adjacency 越界")
        adjacency = struct.unpack_from(f"<{degree}I", buf, 2) if degree else ()
        return DataPayload(degree, adjacency, bytes(buf[adj_end:]))


# ── END Payload (§5.4) ───────────────────────────────────
@dataclass
class EndPayload:
    raw_sha256: bytes

    def pack(self) -> bytes:
        if len(self.raw_sha256) != _SHA256_SIZE:
            raise ProtocolError("raw_sha256 必须 32 字节")
        return self.raw_sha256

    @staticmethod
    def unpack(buf: bytes) -> EndPayload:
        if len(buf) != _SHA256_SIZE:
            raise ProtocolError("end payload 必须正好 32 字节")
        return EndPayload(bytes(buf))


# ── 帧整体编解码 ─────────────────────────────────────────
def encode_frame(header: FrameHeader, payload: bytes) -> bytes:
    """组装完整帧字节流：Header + Payload + CRC32。"""
    header.payload_len = len(payload)
    body = header.pack() + payload
    return body + struct.pack("<I", crc32.calc(body))


def decode_frame(raw: bytes) -> tuple[FrameHeader, bytes]:
    """解析帧并校验 CRC。任何异常抛 ProtocolError，调用方捕获后丢弃。"""
    if len(raw) < HEADER_SIZE + CRC_SIZE:
        raise ProtocolError("帧长度不足最小值")
    header = FrameHeader.unpack(raw[:HEADER_SIZE])
    payload_end = HEADER_SIZE + header.payload_len
    if len(raw) != payload_end + CRC_SIZE:
        raise ProtocolError(
            f"帧长度与 payload_len 不符: got {len(raw)}, expect {payload_end + CRC_SIZE}"
        )
    payload = bytes(raw[HEADER_SIZE:payload_end])
    (crc,) = struct.unpack_from("<I", raw, payload_end)
    if crc != crc32.calc(raw[:payload_end]):
        raise ProtocolError("CRC32 校验失败")
    return header, payload
