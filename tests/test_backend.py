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


def test_enhanced_fallback_does_not_break_normal_decode():
    """三级回退不改变正常帧的返回值，且对无码图仍返回空（不制造假阳性）。"""
    backend = StandardQrBackend()
    payload = b"fallback-check"
    img = backend.encode(payload, ecc_level="L")
    assert backend.decode(img) == [payload]

    blank = Image.new("L", (320, 240), 255)
    assert backend.decode(blank) == []


def test_enhanced_fallback_decodes_downscaled_dense_code():
    """临界分辨率（码点被压到 ~3 px/模块）的密集码仍能解出——第三级兜底存在的意义。"""
    backend = StandardQrBackend()
    payload = bytes(range(200)) * 8
    img = backend.encode(payload, ecc_level="L", box_size=8, border=2)
    modules = (img.size[0] - 2 * 2 * 8) // 8
    side = int(modules * 3.0)
    tiny = img.resize((side, side), Image.BILINEAR)
    assert backend.decode(tiny) == [payload]


def test_decode_ignores_non_qr_symbologies():
    """解码限定标准 QR：画面里的 Code128 等其他码制不再被扫出来（协议只发 QR）。

    同时也锁住性能约束：不限定时 zxing 会对每帧所有码制各扫一遍。
    """
    import zxingcpp

    code = zxingcpp.create_barcode("HELLO-123", zxingcpp.BarcodeFormat.Code128).to_image(scale=4)
    assert zxingcpp.read_barcodes(code), "前提：该图确实含可解 Code128"
    assert StandardQrBackend().decode(code) == []


def test_decode_reads_grid_of_qr_codes():
    """限定码制后多码网格能力不受影响（一次返回多段字节）。"""
    backend = StandardQrBackend()
    p1, p2, p3 = b"grid-a", b"grid-b", b"grid-c"
    imgs = [backend.encode(p, box_size=8, border=2) for p in (p1, p2, p3)]
    w = sum(i.width for i in imgs)
    grid = Image.new("L", (w, max(i.height for i in imgs)), 255)
    x = 0
    for i in imgs:
        grid.paste(i, (x, 0))
        x += i.width
    decoded = backend.decode(grid)
    assert set(decoded) == {p1, p2, p3}
