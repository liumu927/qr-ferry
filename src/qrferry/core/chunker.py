"""数据分块与重组 —— 协议 v1.0 §2.1/§5.2。

源块为定长 chunk_size；最后一块右侧以零字节填充。
定长是 LT 喷泉码按字节异或的前提；重组时按 encoded_size 截断即可还原原始数据。
"""
from __future__ import annotations

__all__ = ["split", "join"]


def split(data: bytes, chunk_size: int) -> list[bytes]:
    """切成定长 chunk_size 的块，最后一块右侧零填充。

    空输入返回空列表（调用方应保证传输内容非空）。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正")
    if not data:
        return []
    blocks: list[bytes] = []
    for i in range(0, len(data), chunk_size):
        block = data[i:i + chunk_size]
        if len(block) < chunk_size:
            block += b"\x00" * (chunk_size - len(block))
        blocks.append(block)
    return blocks


def join(chunks: list[bytes], total_size: int | None = None) -> bytes:
    """拼接所有块；给出 total_size 时截断尾部零填充以还原原始数据。"""
    out = b"".join(chunks)
    if total_size is not None:
        return out[:total_size]
    return out
