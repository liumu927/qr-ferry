"""QR 适配层单元测试。"""
import numpy as np
from PIL import Image

from qrferry.qr.backend import StandardQrBackend


def test_encode_decode_round_trip():
    backend = StandardQrBackend()
    payload = b"\x4f\x51\x01\x02hello binary \xff\x00\x7f" * 3
    img = backend.encode(payload)
    assert isinstance(img, Image.Image)
    assert payload in backend.decode(img)


def test_decode_accepts_numpy_and_pil():
    backend = StandardQrBackend()
    payload = b"qr-ferry protocol v1"
    img = backend.encode(payload)
    assert payload in backend.decode(img)            # PIL 输入
    assert payload in backend.decode(np.array(img))  # numpy 输入


def test_multi_payload_grid():
    """两枚 QR 水平拼成一张图，decode 应一次返回两段字节（多码网格核心能力）。"""
    backend = StandardQrBackend()
    p1, p2 = b"first-payload-aaa", b"second-payload-bbb"
    i1 = backend.encode(p1, box_size=8, border=3)
    i2 = backend.encode(p2, box_size=8, border=3)
    grid = Image.new("L", (i1.width + i2.width, max(i1.height, i2.height)), 255)
    grid.paste(i1, (0, 0))
    grid.paste(i2, (i1.width, 0))
    decoded = backend.decode(grid)
    assert p1 in decoded and p2 in decoded


def test_blank_image_decodes_to_empty():
    assert StandardQrBackend().decode(Image.new("L", (200, 200), 255)) == []


def test_ecc_levels_all_round_trip():
    backend = StandardQrBackend()
    payload = b"x" * 50
    for ecc in ("L", "M", "Q", "H"):
        assert payload in backend.decode(backend.encode(payload, ecc_level=ecc))
