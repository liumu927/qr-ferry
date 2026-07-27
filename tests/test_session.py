"""接收会话状态机单元测试。"""
import hashlib
import zlib

from qrferry.core import chunker, lt
from qrferry.core.frame import (
    Compression,
    ContentType,
    DataPayload,
    FrameHeader,
    FrameType,
    LtDistribution,
    ManifestPayload,
)
from qrferry.core.session import ReceiveSession


def _build_session_frames(data: bytes, chunk_size_log: int, session_id: int,
                          redundancy: float = 2.0):
    """迷你发送端：data → 压缩 → 分块 → LT 编码 → (manifest, [data_payloads])。"""
    encoded = zlib.compress(data)
    chunk_size = 1 << chunk_size_log
    blocks = chunker.split(encoded, chunk_size)
    K = len(blocks)
    manifest = ManifestPayload(
        ContentType.FILE, Compression.ZLIB, chunk_size_log, LtDistribution.RSD,
        K, len(data), len(encoded), hashlib.sha256(data).digest(), "f.bin",
    )
    encoder = lt.LtEncoder(blocks, session_id=session_id)
    payloads = []
    for sid in range(int(K * redundancy)):
        degree, adj, xd = encoder.encode_symbol(sid)
        payloads.append((sid, DataPayload(degree, adj, xd)))
    return manifest, payloads, encoded, data


def test_session_round_trip():
    data = (b"hello qr-ferry " * 20)
    manifest, payloads, encoded, _ = _build_session_frames(data, 4, session_id=42)

    sess = ReceiveSession()
    sess.ingest(FrameHeader(FrameType.MANIFEST, session_id=42), manifest.pack())
    assert sess.started and sess.K == manifest.total_chunks

    for sid, dp in payloads:
        sess.ingest(FrameHeader(FrameType.DATA, session_id=42, symbol_id=sid), dp.pack())
        if sess.is_complete:
            break
    assert sess.is_complete
    assert sess.progress == 1.0
    assert sess.reassemble() == encoded


def test_duplicate_manifest_is_idempotent():
    data = b"abcde" * 50
    manifest, payloads, _, _ = _build_session_frames(data, 4, session_id=7)
    sess = ReceiveSession()
    sess.ingest(FrameHeader(FrameType.MANIFEST, session_id=7), manifest.pack())
    # 喂两个符号
    sess.ingest(FrameHeader(FrameType.DATA, session_id=7, symbol_id=payloads[0][0]),
                payloads[0][1].pack())
    sess.ingest(FrameHeader(FrameType.DATA, session_id=7, symbol_id=payloads[1][0]),
                payloads[1][1].pack())
    p_before = sess.progress
    # 重播 MANIFEST —— 不应重置 decoder
    sess.ingest(FrameHeader(FrameType.MANIFEST, session_id=7), manifest.pack())
    assert sess.progress == p_before


def test_data_for_unknown_session_ignored():
    data = b"x" * 200
    manifest, payloads, _, _ = _build_session_frames(data, 4, session_id=1)
    sess = ReceiveSession()
    sess.ingest(FrameHeader(FrameType.MANIFEST, session_id=1), manifest.pack())
    p0 = sess.progress
    # 另一个 session_id 的 DATA 应被忽略
    sess.ingest(FrameHeader(FrameType.DATA, session_id=999, symbol_id=0),
                payloads[0][1].pack())
    assert sess.progress == p0


def test_data_before_manifest_ignored():
    sess = ReceiveSession()
    dp = DataPayload(1, (0,), b"\x00" * 16)
    sess.ingest(FrameHeader(FrameType.DATA, session_id=1, symbol_id=0), dp.pack())
    assert not sess.started


def test_reassemble_before_complete_raises():
    sess = ReceiveSession()
    try:
        sess.reassemble()
        assert False, "应抛 RuntimeError"
    except RuntimeError:
        pass


def test_progress_increases_monotonically():
    data = bytes(range(256)) * 4
    manifest, payloads, _, _ = _build_session_frames(data, 4, session_id=100)
    sess = ReceiveSession()
    sess.ingest(FrameHeader(FrameType.MANIFEST, session_id=100), manifest.pack())
    last = 0.0
    for sid, dp in payloads:
        sess.ingest(FrameHeader(FrameType.DATA, session_id=100, symbol_id=sid), dp.pack())
        assert sess.progress >= last
        last = sess.progress
        if sess.is_complete:
            break
    assert sess.is_complete


def test_malformed_data_payload_is_ignored_without_crash():
    data = b"x" * 128
    manifest, _, _, _ = _build_session_frames(data, 4, session_id=9)
    sess = ReceiveSession()
    sess.ingest(FrameHeader(FrameType.MANIFEST, session_id=9), manifest.pack())

    bad_adj = DataPayload(1, (999,), b"\x00" * (1 << manifest.chunk_size_log))
    sess.ingest(FrameHeader(FrameType.DATA, session_id=9, symbol_id=1), bad_adj.pack())

    bad_size = DataPayload(1, (0,), b"\x00")
    sess.ingest(FrameHeader(FrameType.DATA, session_id=9, symbol_id=2), bad_size.pack())

    assert sess.progress == 0.0


def test_snapshot_round_trip_resumes_and_completes():
    """喂部分符号 → snapshot → 重建 → 喂剩余 → 完成且数据一致；resolved 在快照中保留。"""
    data = (b"hello qr-ferry " * 30)
    manifest, payloads, encoded, _ = _build_session_frames(data, 4, session_id=42)
    sess = ReceiveSession()
    sess.ingest(FrameHeader(FrameType.MANIFEST, session_id=42), manifest.pack())
    third = len(payloads) // 3
    for sid, dp in payloads[:third]:
        sess.ingest(FrameHeader(FrameType.DATA, session_id=42, symbol_id=sid), dp.pack())
    assert not sess.is_complete, "1/3 符号不应足以完成"
    before_progress = sess.progress
    # snapshot → 重建
    sess2 = ReceiveSession.from_snapshot(sess.to_snapshot())
    assert sess2.session_id == 42
    assert sess2.progress == before_progress      # resolved 种子保留
    assert sess2.missing_indices == sess.missing_indices
    # 喂剩余符号，应能完成
    for sid, dp in payloads[third:]:
        sess2.ingest(FrameHeader(FrameType.DATA, session_id=42, symbol_id=sid), dp.pack())
        if sess2.is_complete:
            break
    assert sess2.is_complete
    assert sess2.reassemble() == encoded


def test_snapshot_none_when_not_started():
    """会话未建立（MANIFEST 未到）时 to_snapshot 返回 None。"""
    assert ReceiveSession().to_snapshot() is None
