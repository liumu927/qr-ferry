"""发送控制器单元测试 —— 验证帧流可被 ReceiveSession 完整接收还原。"""
import random
import zlib

import pytest

from qrferry.app.send_controller import (
    SendController,
    SenderConfig,
    recommend_chunk_size_log,
)
from qrferry.core.frame import (
    Compression,
    ContentType,
    DataPayload,
    FrameType,
    decode_frame,
)
from qrferry.core.session import ReceiveSession


def _rand(n: int, seed: int) -> bytes:
    return random.Random(seed).getrandbits(n * 8).to_bytes(n, "big")


def test_stream_fully_received_and_reassembled():
    data = _rand(5000, 123)
    ctrl = SendController(data, ContentType.FILE, "f.bin", session_id=42,
                         config=SenderConfig(rounds=2, compression=int(Compression.ZLIB)))
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
    common = {"args": (b"x" * 1000, ContentType.TEXT, "", 1)}
    n1 = len(list(SendController(*common["args"], config=SenderConfig(rounds=1))))
    n3 = len(list(SendController(*common["args"], config=SenderConfig(rounds=3))))
    assert n1 < n3
    assert n3 == 3 * (n1 - 1) + 1


def test_auto_chunk_profile_reduces_large_single_code_K():
    data = _rand(600 * 1024, 12)
    ctrl = SendController(data, ContentType.FILE, "large.bin", session_id=3,
                         config=SenderConfig(rounds=1, grid=(1, 1)))
    assert ctrl.config.chunk_size_log == 9
    assert ctrl.K < 1300


def test_auto_chunk_profile_caps_single_code_blocks_at_512_bytes():
    data = _rand(1024 * 1024, 14)
    ctrl = SendController(data, ContentType.FILE, "huge.bin", session_id=5,
                         config=SenderConfig(rounds=1, grid=(1, 1)))
    assert ctrl.config.chunk_size_log == 9
    assert ctrl.K < 2100


def test_auto_chunk_profile_raises_grid_throughput_conservatively():
    assert recommend_chunk_size_log(600 * 1024, grid=(2, 2)) == 9
    assert recommend_chunk_size_log(600 * 1024, grid=(3, 3)) == 8


def test_explicit_chunk_size_overrides_auto_profile():
    data = _rand(600 * 1024, 13)
    ctrl = SendController(data, ContentType.FILE, "large.bin", session_id=4,
                         config=SenderConfig(chunk_size_log=6, rounds=1))
    assert ctrl.config.chunk_size_log == 6


def test_auto_compression_uses_zlib_only_when_smaller():
    compressed = SendController(b"a" * 10_000, ContentType.FILE, "a.bin", session_id=1)
    raw = SendController(_rand(10_000, 19), ContentType.FILE, "random.bin", session_id=2)

    assert compressed.config.compression == int(Compression.ZLIB)
    assert raw.config.compression == int(Compression.NONE)


def test_rolling_frames_send_blocks_in_order():
    ctrl = SendController(
        _rand(600, 20),
        ContentType.FILE,
        "rolling.bin",
        session_id=3,
        config=SenderConfig(
            chunk_size_log=6,
            compression=int(Compression.NONE),
            manifest_interval=100,
        ),
    )
    frames = ctrl.rolling_frames()
    manifest_header, _ = decode_frame(next(frames))
    assert manifest_header.frame_type == FrameType.MANIFEST

    indices = []
    for _ in range(ctrl.K + 1):
        header, payload = decode_frame(next(frames))
        assert header.frame_type == FrameType.DATA
        indices.append(DataPayload.unpack(payload).adjacency[0])

    assert indices == list(range(ctrl.K)) + [0]


def test_rolling_frames_recover_a_chunk_lost_in_first_cycle():
    data = _rand(600, 21)
    ctrl = SendController(
        data,
        ContentType.FILE,
        "rolling.bin",
        session_id=4,
        config=SenderConfig(
            chunk_size_log=6,
            compression=int(Compression.NONE),
            manifest_interval=100,
        ),
    )
    frames = ctrl.rolling_frames()
    sess = ReceiveSession()
    skipped = False
    for _ in range(ctrl.K * 2 + 2):
        header, payload = decode_frame(next(frames))
        if header.frame_type == FrameType.DATA:
            block_index = DataPayload.unpack(payload).adjacency[0]
            if block_index == 3 and not skipped:
                skipped = True
                continue
        sess.ingest(header, payload)
        if sess.is_complete:
            break

    assert sess.is_complete
    assert sess.reassemble() == data


def test_text_payload_has_empty_filename():
    ctrl = SendController(b"hello", ContentType.TEXT, "", session_id=9,
                         config=SenderConfig(rounds=1))
    # 第一帧是 MANIFEST
    header, payload = decode_frame(next(iter(ctrl)))
    from qrferry.core.frame import FrameType, ManifestPayload
    assert header.frame_type == FrameType.MANIFEST
    assert ManifestPayload.unpack(payload).filename == ""


def test_next_data_frame_advances_symbol_id():
    """next_data_frame 推进游标，symbol_id 单调递增；next_sid 反映游标。"""
    from qrferry.core.frame import FrameType
    ctrl = SendController(b"x" * 500, ContentType.TEXT, "", session_id=1,
                         config=SenderConfig(rounds=1))
    sids = []
    for _ in range(5):
        h, _ = decode_frame(ctrl.next_data_frame())
        assert h.frame_type == FrameType.DATA
        sids.append(h.symbol_id)
    assert sids == [0, 1, 2, 3, 4]
    assert ctrl.next_sid == 5


def test_extra_data_frames_negative_rejected():
    ctrl = SendController(b"x" * 100, ContentType.TEXT, "", session_id=1)
    with pytest.raises(ValueError):
        list(ctrl.extra_data_frames(-1))


def test_extra_frames_complete_lossy_transfer():
    """人为丢帧使接收端不完整；补发新符号后完成（验证补发=新 symbol_id，非重发旧帧）。"""
    from qrferry.core.frame import FrameType
    data = _rand(4000, 99)
    ctrl = SendController(data, ContentType.FILE, "f.bin", session_id=77,
                         config=SenderConfig(
                             chunk_size_log=6,
                             rounds=1,
                             compression=int(Compression.ZLIB),
                         ))
    sess = ReceiveSession()
    frames = list(ctrl)   # 消费主帧流，游标推到末尾
    for fb in frames:
        h, p = decode_frame(fb)
        if h.frame_type == FrameType.MANIFEST:
            sess.ingest(h, p)
            break
    # 仅保留 40% 的 DATA 帧（必然不足以恢复 K 个块）
    data_frames = [f for f in frames if decode_frame(f)[0].frame_type == FrameType.DATA]
    keep = data_frames[: len(data_frames) * 2 // 5]
    rng = random.Random(5)
    rng.shuffle(keep)
    for fb in keep:
        h, p = decode_frame(fb)
        sess.ingest(h, p)
    assert not sess.is_complete, "40% 符号不应足以完成恢复"
    # 补发新符号直到完成（游标从主轮末尾继续，产出全新 symbol_id）
    for fb in ctrl.extra_data_frames(ctrl.K * 5):
        h, p = decode_frame(fb)
        sess.ingest(h, p)
        if sess.is_complete:
            break
    assert sess.is_complete
    assert zlib.decompress(sess.reassemble()) == data


def test_sender_snapshot_file_round_trip(tmp_path):
    """FILE 模式 snapshot → 重建：游标保留且续传符号确定一致。"""
    data = _rand(3000, 55)
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    ctrl = SendController(data, ContentType.FILE, "f.bin", session_id=9,
                         config=SenderConfig(chunk_size_log=6, rounds=1),
                         source_kind="file", source_path=str(path))
    for _ in range(10):
        ctrl.next_data_frame()
    assert ctrl.next_sid == 10
    ctrl2 = SendController.from_snapshot(ctrl.to_snapshot())
    assert ctrl2.next_sid == 10
    assert ctrl2.session_id == 9
    # 重建后在同 sid 上产出确定一致的符号（续传根基）
    assert ctrl._encoder.encode_symbol(10) == ctrl2._encoder.encode_symbol(10)


def test_sender_snapshot_preserves_manifest_interval(tmp_path):
    path = tmp_path / "f.bin"
    data = _rand(2000, 18)
    path.write_bytes(data)
    ctrl = SendController(
        data,
        ContentType.FILE,
        "f.bin",
        session_id=12,
        config=SenderConfig(chunk_size_log=9, manifest_interval=8),
        source_kind="file",
        source_path=str(path),
    )

    restored = SendController.from_snapshot(ctrl.to_snapshot())

    assert restored.config.manifest_interval == 8


def test_sender_snapshot_rejects_changed_file(tmp_path):
    """FILE 模式：源文件篡改后 from_snapshot 拒绝（raw_sha 不匹配）。"""
    data = _rand(2000, 8)
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    ctrl = SendController(data, ContentType.FILE, "f.bin", session_id=1,
                         config=SenderConfig(rounds=1),
                         source_kind="file", source_path=str(path))
    snap = ctrl.to_snapshot()
    path.write_bytes(b"tampered content")
    with pytest.raises(ValueError):
        SendController.from_snapshot(snap)


def test_chunk_size_log_for_frame_bytes():
    """帧长上限与源块档位联动：700→512B、1200→1024B、2200→2048B。"""
    from qrferry.app.send_controller import chunk_size_log_for_frame_bytes

    assert chunk_size_log_for_frame_bytes(700) == 9
    assert chunk_size_log_for_frame_bytes(1200) == 10
    assert chunk_size_log_for_frame_bytes(2200) == 11
    with pytest.raises(ValueError):
        chunk_size_log_for_frame_bytes(100)


def test_chunk_size_log_for_frame_bytes_frames_fit():
    """联动档位下，系统化流（degree=1）与满 degree 帧均不超帧长上限。"""
    from qrferry.app.send_controller import chunk_size_log_for_frame_bytes

    for frame_bytes in (700, 1200, 2200):
        data = _rand(64 * 1024, 42)
        ctrl = SendController(
            data, ContentType.FILE, "f.bin", session_id=3,
            config=SenderConfig(
                chunk_size_log=chunk_size_log_for_frame_bytes(frame_bytes),
                max_frame_bytes=frame_bytes, rounds=1, redundancy=1.0,
            ),
        )
        frames = [f for f in ctrl if len(f) > 100]   # DATA 帧（滤掉 MANIFEST/END）
        assert frames
        assert max(len(f) for f in frames) <= frame_bytes
