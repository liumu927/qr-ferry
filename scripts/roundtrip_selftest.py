"""端到端 round-trip 自测 —— M1 收尾。

验证完整协议流水线（不经 QR/摄像头）：
  原始数据 → zlib 压缩 → 定长分块 → LT 喷泉编码 → 封帧
          → 解帧 → LT 解码 → 重组 → 解压 → SHA-256 比对

直接运行: python scripts/roundtrip_selftest.py
退出码: 0=全部通过, 1=存在失败
"""
from __future__ import annotations

import hashlib
import os
import random
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Windows 控制台默认 GBK，强制 UTF-8 输出避免中文/符号编码错误
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from qrferry.core import chunker, lt
from qrferry.core.frame import (
    Compression, ContentType, DataPayload, EndPayload, FrameHeader, FrameType,
    LtDistribution, ManifestPayload, decode_frame, encode_frame,
)
from qrferry.core.session import ReceiveSession

CHUNK_SIZE_LOG = 9    # 512B 源块
REDUNDANCY = 2.0      # 实测兜底（协议 §6.2）


def build_frames(data: bytes, content_type: int, filename: str,
                 session_id: int) -> list[bytes]:
    """发送端流水线：data → [MANIFEST, DATA×, END] 帧字节列表。"""
    encoded = zlib.compress(data, 6)
    chunk_size = 1 << CHUNK_SIZE_LOG
    blocks = chunker.split(encoded, chunk_size)
    K = len(blocks)
    sha = hashlib.sha256(data).digest()

    manifest = ManifestPayload(
        content_type, Compression.ZLIB, CHUNK_SIZE_LOG, LtDistribution.RSD,
        K, len(data), len(encoded), sha, filename,
    )
    frames = [encode_frame(
        FrameHeader(FrameType.MANIFEST, session_id=session_id), manifest.pack())]

    encoder = lt.LtEncoder(blocks, session_id=session_id)
    n_symbols = max(1, int(K * REDUNDANCY))
    for sid in range(n_symbols):
        degree, adj, xd = encoder.encode_symbol(sid)
        dp = DataPayload(degree, adj, xd)
        frames.append(encode_frame(
            FrameHeader(FrameType.DATA, session_id=session_id, symbol_id=sid), dp.pack()))

    frames.append(encode_frame(
        FrameHeader(FrameType.END, session_id=session_id), EndPayload(sha).pack()))
    return frames


def receive(frames: list[bytes]) -> tuple[bytes, ManifestPayload]:
    """接收端流水线：逐帧 decode → ingest → 重组 → 解压。"""
    sess = ReceiveSession()
    for raw in frames:
        header, payload = decode_frame(raw)
        sess.ingest(header, payload)
    if not sess.is_complete:
        raise RuntimeError(f"接收未完成: progress={sess.progress:.1%}")
    data = zlib.decompress(sess.reassemble())
    return data, sess.manifest


def _run_case(name: str, data: bytes, content_type: int, filename: str,
              session_id: int) -> bool:
    frames = build_frames(data, content_type, filename, session_id)
    wire = sum(len(f) for f in frames)
    decoded, manifest = receive(frames)
    sha_ok = hashlib.sha256(decoded).digest() == manifest.raw_sha256
    ok = (decoded == data) and sha_ok
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:<14} "
          f"raw={len(data):>6}B  encoded={manifest.encoded_size:>6}B  "
          f"K={manifest.total_chunks:<4} frames={len(frames):<5} wire={wire:>7}B  "
          f"sha256={'match' if sha_ok else 'MISMATCH'}")
    return ok


def main() -> int:
    print("=== qr-ferry M1 端到端 round-trip 自测 ===\n")
    # 50KB 伪随机（不可压缩），用于真正触发多块 LT + peeling 解码
    big = random.Random(0xDEAD).getrandbits(50_000 * 8).to_bytes(50_000, "big")
    cases = [
        ("文本(短)", "光学二维码摆渡传输系统 · 端到端验证。".encode("utf-8") * 30,
         ContentType.TEXT, "", 0x1111),
        ("文档(中)", b"qr-ferry protocol spec v1.0\n" * 200,
         ContentType.FILE, "spec.txt", 0x2222),
        ("二进制(5KB)", bytes((i * 31 + 7) & 0xFF for i in range(5000)),
         ContentType.FILE, "blob.bin", 0x3333),
        ("大文件(50KB)", big, ContentType.FILE, "big.bin", 0x4444),
    ]
    all_ok = all(_run_case(*c) for c in cases)
    print(f"\n{'全部通过 [PASS]' if all_ok else '存在失败 [FAIL]'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
