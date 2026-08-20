"""压缩编解码 —— app 层封装（zlib 标准库 + zstd 第三方）。

core 层保持「零第三方依赖」，故 ZSTD（`zstandard`）只在此处使用：
- 发送端：`compress_auto` 在 NONE/ZLIB/ZSTD 中取压缩后最小者（等大取靠前枚举，确定性）；
- 接收端：`decompress` 按 MANIFEST 的 compression 字段分派。

ZSTD 级别与 Android 端 `Zstd.compress(data, 9)` 对齐（TextSendController.kt），
保证两端产出同为标准 zstd 帧、互可解压。
"""
from __future__ import annotations

import zlib

from qrferry.core.frame import Compression

__all__ = [
    "ZSTD_LEVEL",
    "compress",
    "compress_auto",
    "compression_name",
    "decompress",
    "zstd_available",
]

ZSTD_LEVEL = 9   # 与 Android 端 TextSendController 的 level 一致

try:
    import zstandard
except ImportError:   # 未安装 zstandard：ZSTD 不可用，自动模式退化为 NONE/ZLIB
    zstandard = None


def zstd_available() -> bool:
    return zstandard is not None


def compression_name(compression: int) -> str:
    """compression 字段 → 展示名（供 UI 状态栏/结果展示）。"""
    return {
        int(Compression.NONE): "无压缩",
        int(Compression.ZLIB): "ZLIB",
        int(Compression.ZSTD): "ZSTD",
    }.get(compression, f"?{compression}")


def _zstd_compress(data: bytes, level: int = ZSTD_LEVEL) -> bytes:
    return zstandard.ZstdCompressor(level=level).compress(data)


def compress(data: bytes, compression: int) -> bytes:
    """按指定 compression 压缩；ZSTD 不可用时抛 ValueError。"""
    if compression == int(Compression.NONE):
        return data
    if compression == int(Compression.ZLIB):
        return zlib.compress(data, 9)
    if compression == int(Compression.ZSTD):
        if zstandard is None:
            raise ValueError("ZSTD 不可用：未安装 zstandard")
        return _zstd_compress(data)
    raise ValueError(f"暂不支持的压缩类型: {compression}")


def compress_auto(data: bytes) -> tuple[int, bytes]:
    """自适应压缩：试 NONE/ZLIB/ZSTD，取压缩后最小者；等大取枚举靠前者（确定性）。

    这是发送端默认策略——文本/XML 等冗余数据自动 ZLIB/ZSTD，
    已压缩内容（PNG/DOCX/PDF）自动回退 NONE 直发，避免浪费时间与带宽。
    """
    candidates = [(int(Compression.NONE), data)]
    candidates.append((int(Compression.ZLIB), zlib.compress(data, 9)))
    if zstandard is not None:
        candidates.append((int(Compression.ZSTD), _zstd_compress(data)))
    return min(candidates, key=lambda t: (len(t[1]), t[0]))


def decompress(compression: int, encoded: bytes) -> bytes:
    """按 compression 解压重组后的字节流。"""
    if compression == int(Compression.NONE):
        return encoded
    if compression == int(Compression.ZLIB):
        return zlib.decompress(encoded)
    if compression == int(Compression.ZSTD):
        if zstandard is None:
            raise ValueError("ZSTD 不可用：未安装 zstandard")
        return zstandard.ZstdDecompressor().decompress(encoded)
    raise ValueError(f"暂不支持的压缩类型: {compression}")
