"""发送控制器单元测试 —— 验证帧流可被 ReceiveSession 完整接收还原。"""
import random
import zlib

import pytest

from qrferry.app.send_controller import SendController, SenderConfig
from qrferry.core.frame import ContentType, decode_frame
from qrferry.core.session import ReceiveSession


def _rand(n: int, seed: int) -> bytes:
    return random.Random(seed).getrandbits(n * 8).to_bytes(n, "big")


def test_stream_fully_received_and_reassembled():
    data = _rand(5000, 123)
    ctrl = SendController(data, ContentType.FILE, "f.bin", session_id=42,
                         config=SenderConfig(rounds=2))
    sess = ReceiveSession()
    count = 0
    for frame_bytes in ctrl:
        header, payload = decode_frame(frame_bytes)
        sess.ingest(header, payload)
        count += 1
    assert sess.is_complete
    assert zlib.decompress(sess.reassemble()) == data
    assert count == ctrl.estimated_frames


def test_empty_data_rejected():
    with pytest.raises(ValueError):
        SendController(b"", ContentType.TEXT, "", session_id=1)


def test_rounds_scale_stream_length():
    common = dict(args=(b"x" * 1000, ContentType.TEXT, "", 1))
    n1 = len(list(SendController(*common["args"], config=SenderConfig(rounds=1))))
    n3 = len(list(SendController(*common["args"], config=SenderConfig(rounds=3))))
    assert n1 < n3
    assert n3 == 3 * n1


def test_text_payload_has_empty_filename():
    ctrl = SendController("hello".encode(), ContentType.TEXT, "", session_id=9,
                         config=SenderConfig(rounds=1))
    # 第一帧是 MANIFEST
    header, payload = decode_frame(next(iter(ctrl)))
    from qrferry.core.frame import FrameType, ManifestPayload
    assert header.frame_type == FrameType.MANIFEST
    assert ManifestPayload.unpack(payload).filename == ""
