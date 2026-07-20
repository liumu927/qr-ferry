"""接收会话断点续传持久化测试 —— 全程不经摄像头，用合成 QR 闭环。"""
import os
import random
import tempfile

from qrferry.app import session_store
from qrferry.app.receive_pipeline import ReceivePipeline
from qrferry.app.send_controller import SendController, SenderConfig
from qrferry.core.frame import ContentType
from qrferry.qr.backend import StandardQrBackend


def _rand(n: int, seed: int) -> bytes:
    return random.Random(seed).getrandbits(n * 8).to_bytes(n, "big")


def test_load_returns_none_when_no_file():
    with tempfile.TemporaryDirectory() as d:
        assert session_store.load(d) is None


def test_load_returns_none_when_corrupted():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".qrferry"), exist_ok=True)
        with open(os.path.join(d, ".qrferry", "pending.json"), "w", encoding="utf-8") as f:
            f.write("{not valid json")
        assert session_store.load(d) is None


def test_clear_removes_file_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, ".qrferry", "pending.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}")
        session_store.clear(d)
        assert not os.path.isfile(p)
        session_store.clear(d)   # 再清不报错


def test_save_load_preserves_progress():
    """save → load 重建的会话 progress 与 missing_indices 一致。"""
    backend = StandardQrBackend()
    with tempfile.TemporaryDirectory() as d:
        frames = list(SendController(_rand(2000, 3), ContentType.FILE, "f.bin", session_id=11,
                                     config=SenderConfig(chunk_size_log=6, rounds=1)))
        pipe = ReceivePipeline(backend=backend, save_dir=d)
        cut = max(1, len(frames) * 2 // 5)
        for fb in frames[:cut]:
            pipe.process_image(backend.encode(fb))
        if pipe.is_complete:
            return   # 极小概率已足够
        session_store.save(pipe.session, d)
        loaded = session_store.load(d)
        assert loaded is not None
        assert loaded.session_id == pipe.session.session_id
        assert abs(loaded.progress - pipe.progress) < 1e-9
        assert loaded.missing_indices == pipe.session.missing_indices


def test_pipeline_resume_completes_after_interrupt():
    """端到端续传：喂 40% 并 save → 新 pipeline resume → 喂剩余 → 完成且数据一致。"""
    backend = StandardQrBackend()
    data = _rand(3000, 7)
    with tempfile.TemporaryDirectory() as d:
        sender = SendController(data, ContentType.FILE, "f.bin", session_id=42,
                                config=SenderConfig(chunk_size_log=6, rounds=1))
        frames = list(sender)
        cut = len(frames) * 2 // 5   # 40%
        pipe1 = ReceivePipeline(backend=backend, save_dir=d)
        for fb in frames[:cut]:
            pipe1.process_image(backend.encode(fb))
        assert not pipe1.is_complete, "40% 帧不应足以完成"
        session_store.save(pipe1.session, d)
        # 模拟中断：丢弃 pipe1，从磁盘恢复
        resumed = session_store.load(d)
        assert resumed is not None and not resumed.is_complete
        pipe2 = ReceivePipeline(backend=backend, save_dir=d, resume_from=resumed)
        for fb in frames[cut:]:
            pipe2.process_image(backend.encode(fb))
            if pipe2.is_complete:
                break
        assert pipe2.is_complete
        assert pipe2.result.data == data
        assert session_store.load(d) is None   # 完成后快照应被清理


def test_save_load_sender_text_round_trip():
    """TEXT 模式 sender 快照往返：原始数据落 bin 后 load_sender 重建可续传。"""
    with tempfile.TemporaryDirectory() as d:
        data = _rand(1500, 21)
        ctrl = SendController(data, ContentType.TEXT, "", session_id=5,
                              config=SenderConfig(chunk_size_log=6, rounds=1),
                              source_kind="text", source_path=None)
        for _ in range(7):
            ctrl.next_data_frame()
        session_store.save_sender(ctrl, d)
        loaded = session_store.load_sender(d)
        assert loaded is not None
        assert loaded.session_id == 5
        assert loaded.next_sid == 7
        assert loaded.raw_data == data


def test_load_sender_returns_none_when_no_file():
    with tempfile.TemporaryDirectory() as d:
        assert session_store.load_sender(d) is None


def test_clear_sender_removes_json_and_bin():
    with tempfile.TemporaryDirectory() as d:
        ctrl = SendController(_rand(800, 4), ContentType.TEXT, "", session_id=3,
                              config=SenderConfig(rounds=1),
                              source_kind="text", source_path=None)
        session_store.save_sender(ctrl, d)
        assert session_store.load_sender(d) is not None
        session_store.clear_sender(d)
        assert session_store.load_sender(d) is None
