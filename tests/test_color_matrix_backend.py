"""彩色矩阵码后端测试。"""
import random
import zlib

import numpy as np
import pytest
from PIL import Image

try:
    import cv2
except ModuleNotFoundError:
    cv2 = None

from qrferry.app.send_controller import (
    COLOR_MATRIX_CHUNK_SIZE_LOG,
    COLOR_MATRIX_MAX_FRAME_BYTES,
    SendController,
    SenderConfig,
)
from qrferry.core.frame import Compression, ContentType, decode_frame
from qrferry.core.session import ReceiveSession
from qrferry.qr.color_matrix import ColorMatrixBackend, ColorMatrixError


def _rand(n: int, seed: int) -> bytes:
    return random.Random(seed).getrandbits(n * 8).to_bytes(n, "big")


def test_color_matrix_round_trip():
    backend = ColorMatrixBackend()
    data = _rand(700, 1)
    img = backend.encode(data)
    assert backend.decode(img) == [data]


def test_color_matrix_uses_lower_density_for_small_payloads():
    backend = ColorMatrixBackend()
    small = backend.encode(b"hello")
    medium = backend.encode(_rand(300, 4))
    large = backend.encode(_rand(700, 5))
    assert small.size == (720, 720)
    assert medium.size == (784, 784)
    assert large.size == (864, 864)
    assert backend.decode(small) == [b"hello"]
    assert backend.decode(medium) == [_rand(300, 4)]
    assert backend.decode(large) == [_rand(700, 5)]


def test_color_matrix_small_payload_padding_is_color_balanced():
    backend = ColorMatrixBackend()
    img = np.array(backend.encode(b"hello"))
    layout = backend._make_layout(32)
    off = layout.data_offset * layout.module_px
    size = layout.grid_size * layout.module_px
    data_area = img[off:off + size, off:off + size]
    palette = np.array(backend._PALETTE)

    counts = []
    for color in palette:
        matches = np.all(data_area == color, axis=2)
        counts.append(int(matches.sum()))

    assert min(counts) > 0
    assert max(counts) / min(counts) < 1.2


def test_color_matrix_rejects_oversized_payload():
    backend = ColorMatrixBackend()
    with pytest.raises(ColorMatrixError):
        backend.encode(b"x" * (backend.max_payload + 1))


def test_color_matrix_decodes_perspective_warp():
    if cv2 is None:
        pytest.skip("OpenCV not installed")
    backend = ColorMatrixBackend()
    data = _rand(700, 2)
    img = np.array(backend.encode(data))
    h, w = img.shape[:2]
    src = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    dst = np.array([[20, 10], [w - 35, 25], [w - 15, h - 30], [35, h - 5]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, matrix, (w, h), borderValue=(255, 255, 255))
    assert backend.decode(warped) == [data]


def test_color_matrix_corrects_small_color_errors():
    backend = ColorMatrixBackend()
    img = np.array(backend.encode(b"hello"))
    layout = backend._make_layout(32)
    off = layout.data_offset * layout.module_px
    px = layout.module_px
    img[off:off + px, off:off + px] = (255, 255, 255)
    assert backend.decode(img) == [b"hello"]


def test_color_matrix_corrects_multiple_codeword_errors():
    backend = ColorMatrixBackend()
    data = _rand(700, 8)
    img = np.array(backend.encode(data))
    layout = backend._make_layout(64)
    off = layout.data_offset
    px = layout.module_px
    calibration_cells = len(backend._CALIBRATION_SYMBOLS)
    for byte_index in range(30):
        cell = calibration_cells + byte_index * 4
        y, x = divmod(cell, layout.grid_size)
        y0 = (off + y) * px
        x0 = (off + x) * px
        img[y0:y0 + px, x0:x0 + px] = backend._PALETTE[(byte_index + 1) % 4]
    assert backend.decode(img) == [data]


def test_color_matrix_uses_calibration_for_camera_color_cast():
    backend = ColorMatrixBackend()
    data = _rand(700, 9)
    img = np.array(backend.encode(data), dtype=np.float32)
    img = img * np.array([0.58, 1.08, 0.72], dtype=np.float32) + np.array(
        [35, 4, 24], dtype=np.float32
    )
    cast = np.clip(img, 0, 255).astype(np.uint8)
    assert backend.decode(cast) == [data]


def test_color_matrix_decodes_rotated_and_resampled_image():
    backend = ColorMatrixBackend()
    data = _rand(700, 10)
    img = backend.encode(data)
    reduced = img.resize((432, 432), Image.Resampling.BILINEAR)
    restored = reduced.resize(img.size, Image.Resampling.BILINEAR)
    rotated = np.rot90(np.array(restored), 2)
    assert backend.decode(rotated) == [data]


def test_color_matrix_loopback_without_camera():
    backend = ColorMatrixBackend()
    data = _rand(4096, 3)
    ctrl = SendController(
        data,
        ContentType.FILE,
        "sample.bin",
        session_id=7,
        config=SenderConfig(
            chunk_size_log=COLOR_MATRIX_CHUNK_SIZE_LOG,
            rounds=1,
            compression=int(Compression.ZLIB),
            max_frame_bytes=COLOR_MATRIX_MAX_FRAME_BYTES,
        ),
    )
    sess = ReceiveSession()
    for frame in ctrl:
        decoded = backend.decode(backend.encode(frame))
        assert decoded, "色码回环应能解出至少一帧"
        for raw in decoded:
            header, payload = decode_frame(raw)
            sess.ingest(header, payload)
    assert sess.is_complete
    assert zlib.decompress(sess.reassemble()) == data


def test_color_matrix_data_frames_use_medium_density():
    backend = ColorMatrixBackend()
    ctrl = SendController(
        _rand(4096, 11),
        ContentType.FILE,
        "sample.bin",
        session_id=8,
        config=SenderConfig(
            chunk_size_log=COLOR_MATRIX_CHUNK_SIZE_LOG,
            max_frame_bytes=COLOR_MATRIX_MAX_FRAME_BYTES,
        ),
    )

    image = backend.encode(ctrl.next_data_frame())

    assert image.size == (864, 864)
