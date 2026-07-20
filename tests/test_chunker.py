"""分块器单元测试。"""
import pytest

from qrferry.core import chunker


def test_split_exact_division():
    assert chunker.split(b"abcdef", 2) == [b"ab", b"cd", b"ef"]


def test_split_pads_last_block():
    blocks = chunker.split(b"abcde", 2)
    assert blocks == [b"ab", b"cd", b"e\x00"]
    assert all(len(b) == 2 for b in blocks)


def test_split_single_short_block():
    assert chunker.split(b"ab", 10) == [b"ab" + b"\x00" * 8]


def test_split_empty_input():
    assert chunker.split(b"", 4) == []


def test_split_invalid_chunk_size():
    with pytest.raises(ValueError):
        chunker.split(b"abc", 0)


def test_join_round_trip_with_trim():
    data = b"abcde"
    assert chunker.join(chunker.split(data, 2), total_size=len(data)) == data


def test_join_without_trim_keeps_padding():
    assert chunker.join(chunker.split(b"abcde", 2)) == b"abcde\x00"


@pytest.mark.parametrize("size", [1, 64, 512, 4096])
def test_all_blocks_fixed_size(size):
    blocks = chunker.split(b"x" * (size * 3 + 1), size)
    assert all(len(b) == size for b in blocks)
    assert len(blocks) == 4
