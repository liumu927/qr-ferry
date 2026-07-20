"""LT 喷泉码单元测试。

覆盖: 度分布归一化、编码确定性、合法 degree/adjacency、
编解码 round-trip（乱序/丢符号/RSD/ISD/K=1）。
"""
import random

import pytest

from qrferry.core.lt import (
    DIST_RSD, DIST_ISD, DIST_DEGENERATE,
    LtEncoder, LtDecoder, _build_degree_cdf,
)


def _blocks(K: int, C: int) -> list[bytes]:
    return [bytes(((i * 13 + j * 7) & 0xFF) for j in range(C)) for i in range(K)]


def test_rsd_cdf_normalized():
    for K in (2, 16, 100, 512):
        cdf = _build_degree_cdf(K, DIST_RSD)
        assert abs(cdf[-1] - 1.0) < 1e-9
        assert cdf[0] == 0.0


def test_isd_cdf_normalized():
    cdf = _build_degree_cdf(50, DIST_ISD)
    assert abs(cdf[-1] - 1.0) < 1e-9


def test_encode_is_deterministic():
    enc = LtEncoder(_blocks(4, 8), session_id=99, dist=DIST_ISD)
    a = enc.encode_symbol(5)
    b = enc.encode_symbol(5)
    assert a == b
    assert a[2] == b[2]   # xor_data 一致


def test_symbol_fields_valid():
    K, C = 8, 16
    enc = LtEncoder(_blocks(K, C), session_id=1, dist=DIST_RSD)
    for sid in range(80):
        degree, adj, xd = enc.encode_symbol(sid)
        assert 1 <= degree <= K
        assert len(adj) == degree
        assert len(set(adj)) == degree          # 去重
        assert all(0 <= i < K for i in adj)     # 范围
        assert adj == tuple(sorted(adj))         # 升序
        assert len(xd) == C


def test_rejects_unequal_blocks():
    with pytest.raises(ValueError):
        LtEncoder([b"aa", b"bbb"], session_id=1)


@pytest.mark.parametrize("dist", [DIST_RSD, DIST_ISD])
def test_round_trip_out_of_order(dist):
    K, C = 32, 64
    original = _blocks(K, C)
    enc = LtEncoder(original, session_id=0xABCD, dist=dist)
    dec = LtDecoder(K, C)

    symbols = [enc.encode_symbol(sid) for sid in range(K * 2)]   # 2× 余量
    rng = random.Random(7)
    rng.shuffle(symbols)        # 乱序注入
    for _degree, adj, xd in symbols:
        dec.add_symbol(adj, xd)
        if dec.is_complete:
            break
    assert dec.is_complete
    assert dec.get_blocks() == original


def test_round_trip_with_losses():
    """编码 2.5K 个符号并随机丢弃 20%，剩余仍应能恢复（体现丢包容忍）。"""
    K, C = 50, 32
    original = _blocks(K, C)
    enc = LtEncoder(original, session_id=2024, dist=DIST_RSD)
    dec = LtDecoder(K, C)
    all_syms = [enc.encode_symbol(sid) for sid in range(int(K * 2.5))]
    rng = random.Random(11)
    rng.shuffle(all_syms)
    for _degree, adj, xd in all_syms[:int(len(all_syms) * 0.8)]:   # 丢弃 20%
        dec.add_symbol(adj, xd)
        if dec.is_complete:
            break
    assert dec.is_complete
    assert dec.get_blocks() == original


def test_single_block():
    enc = LtEncoder([b"only"], session_id=1, dist=DIST_RSD)
    degree, adj, xd = enc.encode_symbol(0)
    assert adj == (0,)
    dec = LtDecoder(1, 4)
    dec.add_symbol(adj, xd)
    assert dec.is_complete
    assert dec.get_blocks() == [b"only"]


def test_degree_capped_for_large_k():
    """大 K 时 degree 受上限约束，DATA 帧不超 QR 安全容量（防 DataOverflowError）。"""
    import zlib
    from qrferry.core import chunker
    from qrferry.core.frame import DataPayload, FrameHeader, FrameType, encode_frame

    data = random.Random(1).getrandbits(500_000 * 8).to_bytes(500_000, "big")
    blocks = chunker.split(zlib.compress(data), 512)
    enc = LtEncoder(blocks, session_id=1, dist=DIST_RSD)
    max_degree = max_frame = 0
    for sid in range(500):
        d, adj, xd = enc.encode_symbol(sid)
        max_degree = max(max_degree, d)
        fb = encode_frame(FrameHeader(FrameType.DATA, 1, symbol_id=sid),
                          DataPayload(d, adj, xd).pack())
        max_frame = max(max_frame, len(fb))
    assert max_degree <= enc._degree_cap
    assert max_frame <= 2000


def test_oversized_block_rejected():
    """block_size 过大（XOR_DATA 单独超 QR 容量）应在构造时报错。"""
    with pytest.raises(ValueError):
        LtEncoder([b"\x00" * 4096, b"\x00" * 4096], session_id=1)
