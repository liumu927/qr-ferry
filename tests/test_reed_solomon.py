import random

import pytest

from qrferry.qr.reed_solomon import ReedSolomonError, decode, encode


def test_reed_solomon_round_trip_and_correction():
    data = random.Random(7).randbytes(231)
    codeword = bytearray(encode(data, 24))
    for index in range(12):
        codeword[index * 17] ^= index + 1
    assert decode(bytes(codeword), 24) == data


def test_reed_solomon_rejects_too_many_errors():
    data = random.Random(8).randbytes(231)
    codeword = bytearray(encode(data, 24))
    for index in range(13):
        codeword[index * 17] ^= index + 1
    with pytest.raises(ReedSolomonError):
        decode(bytes(codeword), 24)
