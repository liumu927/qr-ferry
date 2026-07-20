"""QR 编解码适配层 —— 协议 v1.0 §10。

CodecBackend 抽象接口；StandardQrBackend 用 qrcode + Pillow 编码、zxing-cpp 解码。
未来彩色矩阵码实现同一接口（v2+ 插槽），上层无感知切换。
编码强制字节模式（MODE_8BIT_BYTE），保证协议二进制帧的 round-trip 安全。
"""
from __future__ import annotations

import segno
import zxingcpp
import numpy as np
from io import BytesIO
from PIL import Image

__all__ = ["CodecBackend", "StandardQrBackend"]


class CodecBackend:
    """QR 编解码后端抽象接口。"""

    def encode(self, data: bytes, ecc_level: str = "Q") -> Image.Image:
        raise NotImplementedError

    def decode(self, image) -> list[bytes]:
        """从一张图像解码出若干段字节（支持多码网格）。"""
        raise NotImplementedError


class StandardQrBackend(CodecBackend):
    """标准 QR 后端：qrcode 编码 + zxing-cpp 解码。"""

    _ECC = {"L": "l", "M": "m", "Q": "q", "H": "h"}   # segno 用小写

    def encode(self, data: bytes, ecc_level: str = "Q",
               box_size: int = 10, border: int = 4) -> Image.Image:
        qr = segno.make(data, error=self._ECC[ecc_level], mode="byte")
        buf = BytesIO()
        qr.save(buf, kind="png", scale=box_size, border=border)
        buf.seek(0)
        return Image.open(buf).convert("L")

    def decode(self, image) -> list[bytes]:
        if isinstance(image, Image.Image):
            image = np.array(image)
        # try_rotate/try_invert/try_downscale 默认已开启；显式用 LocalAverage 二值化，
        # 对屏幕拍摄常见的不均匀光照与反光更鲁棒。
        results = zxingcpp.read_barcodes(image, binarizer=zxingcpp.Binarizer.LocalAverage)
        return [r.bytes for r in results if r.bytes]
