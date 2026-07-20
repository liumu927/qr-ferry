"""协议一致性 fixtures 回归测试。

确保入库的 tests/fixtures/protocol/protocol_v1.json 始终与当前协议实现一致。
若协议层改动导致 fixtures 失效，本测试失败，提醒重新运行 scripts/gen_protocol_fixtures.py。
"""
import hashlib
import json
import os
import zlib

from qrferry.core.frame import CRC_SIZE, HEADER_SIZE, MAGIC, VERSION, decode_frame
from qrferry.core.session import ReceiveSession

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "protocol", "protocol_v1.json")


def test_fixtures_exist():
    assert os.path.isfile(FIXTURE_PATH), "fixtures 缺失，请运行 scripts/gen_protocol_fixtures.py"


def test_fixtures_protocol_meta_consistent():
    """fixtures 中的协议常量须与当前实现一致。"""
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        fixture = json.load(f)
    p = fixture["protocol"]
    assert int(p["magic"], 16) == MAGIC
    assert p["version"] == VERSION
    assert p["header_size"] == HEADER_SIZE
    assert p["crc_size"] == CRC_SIZE


def test_fixtures_round_trip_all_cases():
    """每个用例的帧经 decode_frame + ReceiveSession 能完整还原原始数据，SHA-256 一致。"""
    with open(FIXTURE_PATH, encoding="utf-8") as f:
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
