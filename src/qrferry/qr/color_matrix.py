"""彩色矩阵码后端（实验）—— 单屏单码提升帧容量。

该后端复用上层 qrferry 帧协议，只替换 bytes <-> image 的物理编码层。
QFC1 采用 4 色 tile（2 bit/cell）、标定色块、黑色外框和分块 Reed-Solomon，
用于 Android 屏幕发送、PC 摄像头接收的大文件链路。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from qrferry.qr.backend import CodecBackend
from qrferry.qr.reed_solomon import ReedSolomonError, decode as rs_decode, encode as rs_encode

__all__ = ["ColorMatrixBackend", "ColorMatrixError"]


class ColorMatrixError(ValueError):
    """彩色矩阵码编码或解码失败。"""


def _cv2():
    try:
        import cv2
    except ModuleNotFoundError as e:
        raise ColorMatrixError("OpenCV 未安装，无法处理摄像头/屏幕缩放图像") from e
    return cv2


@dataclass(frozen=True)
class _Layout:
    grid_size: int = 64
    module_px: int = 12
    quiet_modules: int = 2
    border_modules: int = 2
    cell_gap_px: int = 1

    @property
    def capacity(self) -> int:
        return (self.grid_size * self.grid_size) // 4

    @property
    def codeword_sizes(self) -> tuple[int, ...]:
        return {
            32: (252,),
            48: (255, 255),
            64: (255, 255, 255, 255),
        }[self.grid_size]

    @property
    def data_capacity(self) -> int:
        return sum(size - ColorMatrixBackend._ECC_BYTES for size in self.codeword_sizes)

    @property
    def data_offset(self) -> int:
        return self.quiet_modules + self.border_modules

    @property
    def total_modules(self) -> int:
        return self.grid_size + 2 * self.data_offset

    @property
    def image_size(self) -> int:
        return self.total_modules * self.module_px


class ColorMatrixBackend(CodecBackend):
    """4 色矩阵码：颜色标定 + 分块 RS 纠错，按负载选择 32/48/64 密度。"""

    _MAGIC = b"QFC1"
    _HEADER_SIZE = 10
    _ECC_BYTES = 64
    _CALIBRATION_SYMBOLS = (0, 1, 2, 3) * 4
    _GRID_SIZES = (32, 48, 64)
    _MODULE_PX = {
        32: 18,
        48: 14,
        64: 12,
    }
    _PALETTE = (
        (248, 113, 113),  # 00 red
        (34, 197, 94),    # 01 green
        (96, 165, 250),   # 10 blue
        (250, 204, 21),   # 11 yellow
    )

    def __init__(self, grid_size: int | None = None, module_px: int | None = None):
        if grid_size is None:
            grid_size = max(self._GRID_SIZES)
        if grid_size <= 0 or grid_size % 2:
            raise ValueError("grid_size 必须为正偶数")
        if grid_size not in self._GRID_SIZES:
            raise ValueError(f"grid_size 必须是 {self._GRID_SIZES} 之一")
        if module_px is None:
            module_px = self._MODULE_PX[grid_size]
        if module_px <= 0:
            raise ValueError("module_px 必须为正")
        self._layout = _Layout(grid_size=grid_size, module_px=module_px)
        self.max_payload = self._layout.data_capacity - self._HEADER_SIZE

    def encode(self, data: bytes, ecc_level: str = "Q") -> Image.Image:
        del ecc_level
        layout = self._select_layout(len(data))

        packet = bytearray(self._balanced_padding(i) for i in range(layout.data_capacity))
        packet[:self._HEADER_SIZE] = (
            self._MAGIC +
            len(data).to_bytes(2, "big") +
            zlib.crc32(data).to_bytes(4, "big")
        )
        packet[self._HEADER_SIZE:self._HEADER_SIZE + len(data)] = data

        img = Image.new("RGB", (layout.image_size, layout.image_size), "white")
        draw = ImageDraw.Draw(img)
        self._draw_border(draw, layout)

        encoded = self._encode_codewords(bytes(packet), layout)
        bits = list(self._CALIBRATION_SYMBOLS) + self._bytes_to_symbols(encoded)
        cells = layout.grid_size * layout.grid_size
        bits.extend(i & 3 for i in range(cells - len(bits)))
        off = layout.data_offset
        px = layout.module_px
        gap = layout.cell_gap_px
        for i, symbol in enumerate(bits):
            y, x = divmod(i, layout.grid_size)
            x0 = (off + x) * px
            y0 = (off + y) * px
            draw.rectangle(
                (x0 + gap, y0 + gap, x0 + px - gap - 1, y0 + px - gap - 1),
                fill=self._PALETTE[symbol],
            )
        return img

    def decode(self, image) -> list[bytes]:
        arr = self._to_rgb_array(image)
        for layout in self._decode_layouts(arr):
            payload = self._decode_with_layout(arr, layout)
            if payload is not None:
                return [payload]
        return []

    def _decode_with_layout(self, arr: np.ndarray, layout: _Layout) -> bytes | None:
        if arr.shape[:2] == (layout.image_size, layout.image_size):
            payload = self._decode_sampled(arr, layout, layout.data_offset)
            if payload is not None:
                return payload

        candidates: list[tuple[np.ndarray, int]] = []
        try:
            candidates.append((self._extract_data_area(arr, layout), 0))
        except ColorMatrixError:
            pass
        try:
            candidates.append((self._extract_code(arr, layout), layout.border_modules))
        except ColorMatrixError:
            pass
        for warped, sample_offset in candidates:
            payload = self._decode_sampled(warped, layout, sample_offset)
            if payload is not None:
                return payload
        return None

    def _decode_sampled(self, arr: np.ndarray, layout: _Layout, offset_modules: int) -> bytes | None:
        for turns in range(4):
            rotated = np.rot90(arr, turns) if turns else arr
            try:
                samples = self._sample_rgb(rotated, layout, offset_modules)
                palette = self._calibrated_palette(samples)
                symbols = self._classify_symbols(samples, palette)
                codeword_size = sum(layout.codeword_sizes)
                encoded = self._symbols_to_bytes(
                    symbols[len(self._CALIBRATION_SYMBOLS):]
                )[:codeword_size]
                packet = self._decode_codewords(encoded, layout)
            except ColorMatrixError:
                continue
            payload = self._payload_from_packet(packet, layout)
            if payload is not None:
                return payload
        return None

    def _select_layout(self, payload_size: int) -> _Layout:
        for grid_size in self._GRID_SIZES:
            layout = self._make_layout(grid_size)
            if payload_size <= layout.data_capacity - self._HEADER_SIZE:
                return layout
        raise ColorMatrixError(
            f"payload={payload_size}B 超出彩色矩阵容量 {self.max_payload}B"
        )

    def _decode_layouts(self, arr: np.ndarray) -> list[_Layout]:
        layouts = [self._make_layout(size) for size in reversed(self._GRID_SIZES)]
        exact = [layout for layout in layouts if arr.shape[:2] == (layout.image_size, layout.image_size)]
        rest = [layout for layout in layouts if layout not in exact]
        return exact + rest

    def _make_layout(self, grid_size: int) -> _Layout:
        if grid_size == self._layout.grid_size:
            return self._layout
        return _Layout(grid_size=grid_size, module_px=self._MODULE_PX[grid_size])

    def _payload_from_packet(self, packet: bytes, layout: _Layout) -> bytes | None:
        if packet[:4] != self._MAGIC:
            return None
        size = int.from_bytes(packet[4:6], "big")
        if size > layout.data_capacity - self._HEADER_SIZE:
            return None
        crc = int.from_bytes(packet[6:10], "big")
        payload = packet[self._HEADER_SIZE:self._HEADER_SIZE + size]
        return payload if zlib.crc32(payload) == crc else None

    def _draw_border(self, draw: ImageDraw.ImageDraw, layout: _Layout) -> None:
        q = layout.quiet_modules
        b = layout.border_modules
        px = layout.module_px
        end = layout.total_modules - q
        draw.rectangle((q * px, q * px, end * px - 1, end * px - 1), fill="black")
        inner0 = (q + b) * px
        inner1 = (end - b) * px
        draw.rectangle((inner0, inner0, inner1 - 1, inner1 - 1), fill="white")

    def _extract_code(self, arr: np.ndarray, layout: _Layout) -> np.ndarray:
        cv2 = _cv2()
        border_modules = layout.grid_size + 2 * layout.border_modules
        size = border_modules * layout.module_px
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        mask = np.where((val < 105) & (sat < 90), 255, 0).astype(np.uint8)
        kernel = np.ones((3, 3), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _hier = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            raise ColorMatrixError("未找到彩色矩阵外框")
        contour = max(contours, key=cv2.contourArea)
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
        if len(approx) == 4:
            box = approx.reshape(4, 2).astype(np.float32)
        else:
            rect = cv2.minAreaRect(contour)
            box = cv2.boxPoints(rect).astype(np.float32)
        box = self._order_points(box)
        dst = np.array([[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]], dtype=np.float32)
        matrix = cv2.getPerspectiveTransform(box, dst)
        return cv2.warpPerspective(arr, matrix, (size, size))

    def _extract_data_area(self, arr: np.ndarray, layout: _Layout) -> np.ndarray:
        cv2 = _cv2()
        data_size = layout.grid_size * layout.module_px
        hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        mask = np.where((sat > 55) & (val > 70), 255, 0).astype(np.uint8)
        kernel = np.ones((9, 9), dtype=np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _hier = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            raise ColorMatrixError("未找到彩色矩阵数据区")
        contour = max(contours, key=cv2.contourArea)
        rect = cv2.minAreaRect(contour)
        box = self._order_points(cv2.boxPoints(rect).astype(np.float32))
        dst = np.array(
            [[0, 0], [data_size - 1, 0], [data_size - 1, data_size - 1], [0, data_size - 1]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(box, dst)
        return cv2.warpPerspective(arr, matrix, (data_size, data_size))

    def _sample_rgb(self, arr: np.ndarray, layout: _Layout, offset_modules: int | None = None) -> np.ndarray:
        off = offset_modules
        if off is None:
            off = (
                layout.data_offset
                if arr.shape[0] == layout.image_size
                else layout.border_modules
            )
        px = layout.module_px
        samples: list[np.ndarray] = []
        for y in range(layout.grid_size):
            for x in range(layout.grid_size):
                x0 = int((off + x + 0.3) * px)
                x1 = int((off + x + 0.7) * px)
                y0 = int((off + y + 0.3) * px)
                y1 = int((off + y + 0.7) * px)
                rgb = arr[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0).astype(np.float32)
                samples.append(rgb)
        return np.asarray(samples, dtype=np.float32)

    def _sample_symbols(self, arr: np.ndarray, layout: _Layout, offset_modules: int | None = None) -> list[int]:
        samples = self._sample_rgb(arr, layout, offset_modules)
        return self._classify_symbols(samples, np.asarray(self._PALETTE, dtype=np.float32))

    @classmethod
    def _calibrated_palette(cls, samples: np.ndarray) -> np.ndarray:
        if len(samples) < len(cls._CALIBRATION_SYMBOLS):
            raise ColorMatrixError("彩色矩阵标定区不完整")
        groups = [[] for _ in cls._PALETTE]
        for index, symbol in enumerate(cls._CALIBRATION_SYMBOLS):
            groups[symbol].append(samples[index])
        return np.asarray([np.mean(group, axis=0) for group in groups], dtype=np.float32)

    @staticmethod
    def _classify_symbols(samples: np.ndarray, palette: np.ndarray) -> list[int]:
        distances = np.sum((samples[:, None, :] - palette[None, :, :]) ** 2, axis=2)
        return np.argmin(distances, axis=1).astype(int).tolist()

    @classmethod
    def _encode_codewords(cls, packet: bytes, layout: _Layout) -> bytes:
        out = bytearray()
        offset = 0
        for size in layout.codeword_sizes:
            data_size = size - cls._ECC_BYTES
            out.extend(rs_encode(packet[offset:offset + data_size], cls._ECC_BYTES))
            offset += data_size
        return bytes(out)

    @classmethod
    def _decode_codewords(cls, encoded: bytes, layout: _Layout) -> bytes:
        out = bytearray()
        offset = 0
        try:
            for size in layout.codeword_sizes:
                block = encoded[offset:offset + size]
                if len(block) != size:
                    raise ColorMatrixError("彩色矩阵码字长度不足")
                out.extend(rs_decode(block, cls._ECC_BYTES))
                offset += size
        except (ReedSolomonError, ValueError) as error:
            raise ColorMatrixError("彩色矩阵纠错失败") from error
        return bytes(out)

    @staticmethod
    def _to_rgb_array(image) -> np.ndarray:
        if isinstance(image, Image.Image):
            return np.array(image.convert("RGB"))
        arr = np.asarray(image)
        if arr.ndim == 2:
            cv2 = _cv2()
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
        if arr.shape[2] == 4:
            arr = arr[:, :, :3]
        return arr.astype(np.uint8)

    @staticmethod
    def _bytes_to_symbols(data: bytes) -> list[int]:
        out: list[int] = []
        for b in data:
            out.extend(((b >> 6) & 3, (b >> 4) & 3, (b >> 2) & 3, b & 3))
        return out

    @staticmethod
    def _symbols_to_bytes(symbols: list[int]) -> bytes:
        out = bytearray()
        for i in range(0, len(symbols), 4):
            a, b, c, d = symbols[i:i + 4]
            out.append((a << 6) | (b << 4) | (c << 2) | d)
        return bytes(out)

    @staticmethod
    def _balanced_padding(index: int) -> int:
        a = index & 0x03
        b = (index + 1) & 0x03
        c = (index + 2) & 0x03
        d = (index + 3) & 0x03
        return (a << 6) | (b << 4) | (c << 2) | d

    @staticmethod
    def _order_points(points: np.ndarray) -> np.ndarray:
        s = points.sum(axis=1)
        diff = np.diff(points, axis=1).reshape(-1)
        return np.array([
            points[np.argmin(s)],
            points[np.argmin(diff)],
            points[np.argmax(s)],
            points[np.argmax(diff)],
        ], dtype=np.float32)
