"""生成协议一致性 fixtures —— 供 Android 等跨语言实现逐字节对照。

确定性根基：LtEncoder.encode_symbol 对 (session_id, symbol_id) 确定映射，
故固定 session_id + 相同输入，任意平台生成的帧字节必须与本 fixtures 完全一致。

运行：
    .venv/Scripts/python.exe scripts/gen_protocol_fixtures.py

产出：tests/fixtures/protocol/protocol_v1.json（含协议常量 + 多个用例的帧 hex + 符号详情）。
生成后自动回读自验：每个用例经 decode_frame + ReceiveSession 能完整还原原始数据。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qrferry.app.send_controller import SendController, SenderConfig
from qrferry.core.frame import (
    CRC_SIZE, HEADER_SIZE, MAGIC, VERSION,
    Compression, ContentType, FrameType, LtDistribution,
    DataPayload, decode_frame,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "protocol")


def _protocol_meta() -> dict:
    return {
        "protocol_version": "v1.0",
        "byte_order": "little-endian",
        "frame_layout": "Header(18) + Payload(variable) + CRC32(4)",
        "magic": f"0x{MAGIC:04X}",
        "version": VERSION,
        "header_size": HEADER_SIZE,
        "crc_size": CRC_SIZE,
        "header_fmt": "<HBBIIIH",
        "header_fields": [
            "MAGIC(u16)", "VERSION(u8)", "TYPE(u8)", "SESSION(u32)",
            "STREAM(u32)", "SYMBOL(u32)", "PAYLOAD_LEN(u16)",
        ],
        "frame_types": {t.name: int(t) for t in FrameType},
        "content_types": {t.name: int(t) for t in ContentType},
        "compressions": {c.name: int(c) for c in Compression},
        "lt_distributions": {d.name: int(d) for d in LtDistribution},
    }


def _build_case(name: str, data: bytes, content_type: int, filename: str,
                session_id: int, chunk_size_log: int) -> dict:
    cfg = SenderConfig(chunk_size_log=chunk_size_log, compression=int(Compression.ZLIB),
                       lt_dist=int(LtDistribution.RSD), redundancy=2.0, rounds=1)
    ctrl = SendController(data, content_type, filename, session_id, cfg)
    manifest_hex = end_hex = None
    symbols: list[dict] = []
    for fb in ctrl:
        h, p = decode_frame(fb)
        if h.frame_type == FrameType.MANIFEST:
            manifest_hex = fb.hex()
        elif h.frame_type == FrameType.END:
            end_hex = fb.hex()
        elif h.frame_type == FrameType.DATA:
            dp = DataPayload.unpack(p)
            symbols.append({
                "symbol_id": h.symbol_id,
                "degree": dp.degree,
                "adjacency": list(dp.adjacency),
                "xor_data_hex": dp.xor_data.hex(),
                "frame_hex": fb.hex(),
            })
    case = {
        "name": name,
        "session_id": session_id,
        "content_type": int(content_type),
        "compression": int(Compression.ZLIB),
        "lt_dist": int(LtDistribution.RSD),
        "chunk_size_log": chunk_size_log,
        "redundancy": cfg.redundancy,
        "rounds": cfg.rounds,
        "K": ctrl.K,
        "raw_size": ctrl.raw_size,
        "encoded_size": len(ctrl._encoded),
        "raw_sha256": hashlib.sha256(data).hexdigest(),
        "input_hex": data.hex(),
        "input_text": data.decode("utf-8") if content_type == ContentType.TEXT else None,
        "filename": filename,
        "manifest_frame_hex": manifest_hex,
        "end_frame_hex": end_hex,
        "data_symbols": symbols,
    }
    return case


def _verify(path: str) -> None:
    """回读 fixtures，逐用例 decode_frame + ReceiveSession 还原，断言数据一致。"""
    from qrferry.core.session import ReceiveSession
    with open(path, encoding="utf-8") as f:
        fixture = json.load(f)
    for case in fixture["cases"]:
        data = bytes.fromhex(case["input_hex"])
        sess = ReceiveSession()
        frame_hexes = ([case["manifest_frame_hex"]]
                       + [s["frame_hex"] for s in case["data_symbols"]]
                       + [case["end_frame_hex"]])
        for fb_hex in frame_hexes:
            h, p = decode_frame(bytes.fromhex(fb_hex))
            sess.ingest(h, p)
            if sess.is_complete:
                break
        assert sess.is_complete, f"用例 {case['name']} 未能完成还原"
        assert zlib.decompress(sess.reassemble()) == data, f"用例 {case['name']} 数据不一致"
        assert hashlib.sha256(data).hexdigest() == case["raw_sha256"]


def main() -> None:
    cases = [
        _build_case("text_short", b"hello qr-ferry",
                    int(ContentType.TEXT), "", session_id=42, chunk_size_log=4),
        _build_case("text_unicode", "你好，光学摆渡".encode("utf-8"),
                    int(ContentType.TEXT), "", session_id=100, chunk_size_log=4),
        _build_case("file_binary", random.Random(1234).randbytes(3000),
                    int(ContentType.FILE), "f.bin", session_id=200, chunk_size_log=6),
    ]
    fixture = {
        "description": "qr-ferry 协议 v1.0 字节级黄金参考；跨语言实现须逐字节复现",
        "generated_by": "scripts/gen_protocol_fixtures.py",
        "protocol": _protocol_meta(),
        "cases": cases,
    }
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    out = os.path.join(FIXTURE_DIR, "protocol_v1.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=2)
    print(f"已生成 {len(cases)} 个用例 → {out}")
    _verify(out)
    print("自验通过：所有用例经 decode_frame + ReceiveSession 可完整还原")


if __name__ == "__main__":
    main()
