"""帧编解码单元测试。

覆盖: Header/MANIFEST/DATA/END 各自 round-trip、安全约束（坏 MAGIC/超长文件名/
degree 不符）、帧整体 CRC 完整性（篡改/截断/粘包一律拒绝）。
"""
import pytest

from qrferry.core.frame import (
    VERSION, FrameHeader, FrameType, ContentType, Compression, LtDistribution,
    ManifestPayload, DataPayload, EndPayload,
    ProtocolError, encode_frame, decode_frame,
)


def _sha(n: int = 32) -> bytes:
    return bytes((i * 7 + 3) & 0xFF for i in range(n))


# ── Header ───────────────────────────────────────────────
def test_header_round_trip():
    h = FrameHeader(frame_type=FrameType.DATA, session_id=0x12345678,
                    stream_id=2, symbol_id=0xAB, payload_len=512)
    raw = h.pack()
    assert len(raw) == 18
    h2 = FrameHeader.unpack(raw)
    assert (h2.frame_type, h2.session_id, h2.stream_id, h2.symbol_id,
            h2.payload_len, h2.version) == (FrameType.DATA, 0x12345678, 2, 0xAB, 512, VERSION)


def test_header_bad_magic_rejected():
    raw = bytearray(FrameHeader(frame_type=1, session_id=1).pack())
    raw[0] = 0x00   # 破坏 MAGIC
    with pytest.raises(ProtocolError):
        FrameHeader.unpack(bytes(raw))


# ── MANIFEST ─────────────────────────────────────────────
def test_manifest_round_trip_file():
    m = ManifestPayload(
        content_type=ContentType.FILE, compression=Compression.ZLIB,
        chunk_size_log=9, lt_dist=LtDistribution.RSD, total_chunks=42,
        raw_size=10000, encoded_size=6000, raw_sha256=_sha(),
        filename="报告.pdf",
    )
    m2 = ManifestPayload.unpack(m.pack())
    assert (m2.filename, m2.total_chunks, m2.raw_size, m2.encoded_size) == ("报告.pdf", 42, 10000, 6000)
    assert m2.raw_sha256 == _sha()
    assert m2.content_type == ContentType.FILE


def test_manifest_text_has_empty_filename():
    m = ManifestPayload(ContentType.TEXT, Compression.NONE, 9, LtDistribution.RSD,
                        1, 5, 5, _sha(), filename="")
    assert ManifestPayload.unpack(m.pack()).filename == ""


def test_manifest_filename_too_long_rejected():
    m = ManifestPayload(1, 1, 9, 0, 1, 1, 1, _sha(), filename="x" * 256)
    with pytest.raises(ProtocolError):
        m.pack()


def test_manifest_min_size_is_57_for_text():
    # 协议 §5.2: TEXT 最小 57B（24 固定 + 1 文件名长度 + 0 文件名 + 32 SHA）
    m = ManifestPayload(ContentType.TEXT, Compression.NONE, 9, LtDistribution.RSD,
                        1, 5, 5, _sha(), filename="")
    assert len(m.pack()) == 57


# ── DATA ─────────────────────────────────────────────────
def test_data_round_trip():
    d = DataPayload(degree=3, adjacency=(0, 5, 9), xor_data=b"\x01\x02\x03")
    d2 = DataPayload.unpack(d.pack())
    assert (d2.degree, d2.adjacency, d2.xor_data) == (3, (0, 5, 9), b"\x01\x02\x03")


def test_data_degree_mismatch_rejected():
    with pytest.raises(ProtocolError):
        DataPayload(degree=2, adjacency=(1,), xor_data=b"x").pack()


# ── END ──────────────────────────────────────────────────
def test_end_round_trip():
    e2 = EndPayload.unpack(EndPayload(_sha()).pack())
    assert e2.raw_sha256 == _sha()


# ── 帧整体 ───────────────────────────────────────────────
def test_frame_round_trip_data():
    payload = DataPayload(degree=1, adjacency=(7,), xor_data=b"hello").pack()
    raw = encode_frame(
        FrameHeader(frame_type=FrameType.DATA, session_id=0xCAFE, symbol_id=99),
        payload,
    )
    h2, p2 = decode_frame(raw)
    assert (h2.frame_type, h2.session_id, h2.symbol_id) == (FrameType.DATA, 0xCAFE, 99)
    d2 = DataPayload.unpack(p2)
    assert (d2.adjacency, d2.xor_data) == ((7,), b"hello")


def test_frame_crc_tamper_detected():
    raw = bytearray(encode_frame(
        FrameHeader(frame_type=FrameType.END, session_id=1), b"payload data"))
    raw[20] ^= 0xFF   # 篡改 payload 区
    with pytest.raises(ProtocolError):
        decode_frame(bytes(raw))


def test_frame_truncated_rejected():
    raw = encode_frame(FrameHeader(frame_type=FrameType.END, session_id=1), b"x")[:-1]
    with pytest.raises(ProtocolError):
        decode_frame(raw)


def test_frame_extra_bytes_rejected():
    raw = encode_frame(FrameHeader(frame_type=FrameType.END, session_id=1), b"x") + b"\x00"
    with pytest.raises(ProtocolError):
        decode_frame(raw)
