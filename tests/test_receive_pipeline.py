"""接收流水线单元测试 —— 用合成 QR 图验证全闭环（不经摄像头）。"""
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


def test_pipeline_file_round_trip():
    data = _rand(3000, 7)
    sender = SendController(data, ContentType.FILE, "报告.pdf", session_id=42,
                            config=SenderConfig(chunk_size_log=6, rounds=2))
    with tempfile.TemporaryDirectory() as d:
        pipe = ReceivePipeline(backend=StandardQrBackend(), save_dir=d)
        _drive(sender, pipe)
        assert pipe.is_complete and pipe.result is not None
        assert pipe.result.path is not None
        with open(pipe.result.path, "rb") as f:
            assert f.read() == data


def test_pipeline_text_no_file_written():
    text = "你好 qr-ferry".encode("utf-8")
    sender = SendController(text, ContentType.TEXT, "", session_id=5,
                            config=SenderConfig(chunk_size_log=6, rounds=1))
    pipe = ReceivePipeline(backend=StandardQrBackend())
    _drive(sender, pipe)
    assert pipe.result is not None
    assert pipe.result.data == text
    assert pipe.result.path is None


def test_pipeline_supports_uncompressed_payload():
    text = "不压缩传输".encode("utf-8")
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
    assert not pipe.is_complete
