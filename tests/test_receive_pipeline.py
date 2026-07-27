"""接收流水线单元测试 —— 用合成 QR 图验证全闭环（不经摄像头）。"""
import os
import random
import tempfile

from PIL import Image

from qrferry.app.receive_pipeline import ReceivePipeline, safe_filename
from qrferry.app.send_controller import SendController, SenderConfig
from qrferry.core.frame import ContentType
from qrferry.qr.backend import StandardQrBackend


def _rand(n: int, seed: int) -> bytes:
    return random.Random(seed).getrandbits(n * 8).to_bytes(n, "big")


def _drive(sender: SendController, pipe: ReceivePipeline) -> None:
    """把每帧字节编码成 QR 图喂给 pipeline，直到完成。"""
    backend = pipe.backend
    for fb in sender:
        pipe.process_image(backend.encode(fb))
        if pipe.is_complete:
            break


def test_pipeline_file_result_stays_in_memory_until_saved():
    data = _rand(3000, 7)
    sender = SendController(data, ContentType.FILE, "报告.pdf", session_id=42,
                            config=SenderConfig(chunk_size_log=6, rounds=2))
    with tempfile.TemporaryDirectory() as d:
        pipe = ReceivePipeline(backend=StandardQrBackend(), save_dir=d)
        _drive(sender, pipe)
        assert pipe.is_complete and pipe.result is not None
        assert pipe.result.filename == "报告.pdf"
        assert pipe.result.data == data
        assert not os.path.exists(os.path.join(d, "报告.pdf"))


def test_pipeline_text_no_file_written():
    text = "你好 qr-ferry".encode()
    sender = SendController(text, ContentType.TEXT, "", session_id=5,
                            config=SenderConfig(chunk_size_log=6, rounds=1))
    pipe = ReceivePipeline(backend=StandardQrBackend())
    _drive(sender, pipe)
    assert pipe.result is not None
    assert pipe.result.data == text


def test_pipeline_supports_uncompressed_payload():
    text = "不压缩传输".encode()
    sender = SendController(text, ContentType.TEXT, "", session_id=6,
                            config=SenderConfig(chunk_size_log=6, rounds=1, compression=0))
    pipe = ReceivePipeline(backend=StandardQrBackend())
    _drive(sender, pipe)
    assert pipe.result is not None
    assert pipe.result.data == text


def test_safe_filename_strips_traversal():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("a/b\\c.txt") == "c.txt"
    assert safe_filename("") == "received"
    assert safe_filename('a<b>c:"d|e?f*g') == "a_b_c__d_e_f_g"


def test_blank_image_yields_zero_and_no_crash():
    pipe = ReceivePipeline(backend=StandardQrBackend())
    blank = Image.new("L", (200, 200), 255)
    assert pipe.process_image(blank) == 0
    assert pipe.missed_images == 1
    assert not pipe.is_complete


def test_pipeline_counts_valid_frames_in_round_trip():
    data = _rand(2000, 2)
    sender = SendController(data, ContentType.FILE, "f.bin", session_id=1,
                            config=SenderConfig(chunk_size_log=6, rounds=1))
    with tempfile.TemporaryDirectory() as d:
        pipe = ReceivePipeline(backend=StandardQrBackend(), save_dir=d)
        _drive(sender, pipe)
        assert pipe.is_complete
        assert pipe.valid_frames > 0
        assert pipe.bad_frames == 0      # 合成 QR 无坏帧
        assert pipe.drop_rate == 0.0


def test_pipeline_drop_rate_with_bad_frames():
    """CRC 失败帧计入 bad_frames，drop_rate = bad/(valid+bad)。"""
    from qrferry.core.frame import FrameType, decode_frame
    data = _rand(1500, 1)
    sender = SendController(data, ContentType.FILE, "f.bin", session_id=1,
                            config=SenderConfig(chunk_size_log=6, rounds=1))
    frames = list(sender)
    manifest_frame = next(f for f in frames if decode_frame(f)[0].frame_type == FrameType.MANIFEST)
    data_frame = next(f for f in frames if decode_frame(f)[0].frame_type == FrameType.DATA)

    class _MockBackend:
        def decode(self, image):
            return self._queue
        def encode(self, *a, **k):
            return None
    mb = _MockBackend()
    pipe = ReceivePipeline(backend=mb)
    mb._queue = [manifest_frame]
    pipe.process_image(None)
    mb._queue = [data_frame]
    pipe.process_image(None)
    assert pipe.valid_frames == 2
    assert pipe.bad_frames == 0
    # 注入坏 bytes（MAGIC 不匹配）→ decode_frame 抛 ProtocolError → 计入 bad
    mb._queue = [b"\x00" * 30]
    pipe.process_image(None)
    assert pipe.bad_frames == 1
    assert abs(pipe.drop_rate - 1 / 3) < 1e-9   # 1 bad / 3 total
