"""CRC32 单元测试。

覆盖:
- 协议 §13 golden 向量
- 空输入与边界字节
- 与 zlib.crc32 全等价（跨端实现正确性的基准）
"""
import zlib

import pytest

from qrferry.core import crc32


def test_golden_vector():
    """协议 §13: b'123456789' -> 0xCBF43926（IEEE 标准校验向量）。"""
    assert crc32.calc(b"123456789") == 0xCBF43926


def test_empty_input():
    assert crc32.calc(b"") == 0x00000000


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"a",
        b"\x00",
        b"\xff",
        b"123456789",
        b"The quick brown fox jumps over the lazy dog",
        bytes(range(256)),
        b"abcdefghijklmnopqrstuvwxyz" * 100,
    ],
)
def test_matches_zlib(data):
    """自实现必须与标准库 zlib.crc32 逐位一致。"""
    assert crc32.calc(data) == zlib.crc32(data)
