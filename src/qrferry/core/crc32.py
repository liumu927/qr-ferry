"""CRC32 (IEEE 802.3) —— 帧校验用。

与 zlib.crc32 / ZIP CRC32 完全一致；跨端移植（Kotlin/Rust）时按本实现。

参数:
    多项式（反射）: 0xEDB88320
    初始值:        0xFFFFFFFF
    输出异或:      0xFFFFFFFF
    输入/输出:     非反射字节流 / uint32
"""

__all__ = ["calc"]


def _build_table() -> tuple[int, ...]:
    """预计算 256 项 CRC 查找表。"""
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if (c & 1) else (c >> 1)
        table.append(c & 0xFFFFFFFF)
    return tuple(table)


# 模块加载时构建一次，后续调用零开销查表。
_TABLE: tuple[int, ...] = _build_table()


def calc(data: bytes) -> int:
    """计算 data 的 CRC32，返回 [0, 2**32-1] 区间的无符号整数。"""
    c = 0xFFFFFFFF
    for b in data:
        c = _TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF
