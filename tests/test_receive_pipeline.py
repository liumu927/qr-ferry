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


def test_new_session_resets_elapsed_time_not_accumulated():
    """接收中途来了新一批码（不同 session_id）：进度归零时「已用」也必须归零。

    回归背景：用户不点停止直接换下一批码时，进度条回到 0% 但「已用」接着累计，
    显示成「一个 3 秒的文件用了 40 秒」，时长/速率全部失真。
    """
    import time as _t

    backend = StandardQrBackend()
    pipe = ReceivePipeline(backend=backend, persist=False)

    # 第一次传输：跑一部分帧，让计时基准建立
    s1 = SendController(_rand(3000, 1), ContentType.FILE, "a.bin", session_id=101,
                        config=SenderConfig(chunk_size_log=6, redundancy=2.0, rounds=2))
    it1 = iter(s1)
    for _ in range(3):
        pipe.process_image(backend.encode(next(it1)))
    first_elapsed = pipe.elapsed_seconds
    assert first_elapsed >= 0.0
    _t.sleep(0.05)
    assert pipe.elapsed_seconds > first_elapsed, "同一会话内时间应继续累计"

    # 第二批码：不同 session_id → 全新传输
    s2 = SendController(_rand(3000, 2), ContentType.FILE, "b.bin", session_id=202,
                        config=SenderConfig(chunk_size_log=6, redundancy=2.0, rounds=2))
    pipe.process_image(backend.encode(next(iter(s2))))

    assert pipe.session.session_id == 202
    assert pipe.elapsed_seconds < 0.05, f"换批后「已用」应重新计时，实际 {pipe.elapsed_seconds}"
    assert pipe.valid_data_frames <= 1, "换批后有效帧计数应重置"


def test_new_session_after_completion_can_complete_again():
    """上一份文件已完成后又来新一批码：结果需复位，并且新文件能再次触发完成。"""
    backend = StandardQrBackend()
    pipe = ReceivePipeline(backend=backend, persist=False)

    data1 = _rand(1200, 11)
    _drive(SendController(data1, ContentType.FILE, "one.bin", session_id=1,
                          config=SenderConfig(chunk_size_log=6, rounds=2)), pipe)
    assert pipe.is_complete and pipe.result is not None
    assert pipe.result.filename == "one.bin"

    data2 = _rand(1500, 12)
    _drive(SendController(data2, ContentType.FILE, "two.bin", session_id=2,
                          config=SenderConfig(chunk_size_log=6, rounds=2)), pipe)
    assert pipe.is_complete
    assert pipe.result is not None and pipe.result.filename == "two.bin", "应产出新文件结果"
    assert pipe.result.data == data2


def test_elapsed_seconds_starts_at_zero_before_first_frame():
    """尚未收到任何有效帧时「已用」应为 0（而不是从管道创建时刻起算）。"""
    pipe = ReceivePipeline(backend=StandardQrBackend(), persist=False)
    assert pipe.elapsed_seconds == 0.0


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


def test_pipeline_zstd_payload_round_trip():
    """ZSTD 压缩的载荷经流水线重组解压后还原，且结果携带压缩信息。"""
    data = (b"<item><name>qr-ferry</name><value>0123456789</value></item>\n" * 200)
    sender = SendController(data, ContentType.FILE, "data.xml", session_id=9,
                            config=SenderConfig(chunk_size_log=6, rounds=1,
                                                compression=2))  # Compression.ZSTD
    pipe = ReceivePipeline(backend=StandardQrBackend())
    _drive(sender, pipe)
    assert pipe.result is not None
    assert pipe.result.data == data
    assert pipe.result.compression == 2
    assert pipe.result.raw_size == len(data)
    assert pipe.result.encoded_size < len(data)


def test_safe_filename_strips_traversal():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("a/b\\c.txt") == "c.txt"
    assert safe_filename("") == "received"
    assert safe_filename('a<b>c:"d|e?f*g') == "a_b_c__d_e_f_g"


def test_blank_image_yields_zero_and_no_crash():
    pipe = ReceivePipeline(backend=StandardQrBackend())
    blank = Image.new("L", (200, 200), 255)
    assert pipe.process_image(blank) == 0


def test_first_frame_ts_not_set_by_pre_manifest_data():
    """回归：中途加入流的接收（如断点续传弹窗停留后），MANIFEST 前到达的 DATA 帧
    被 session.ingest 静默忽略，不得打上 first_valid_frame_ts——否则统计行
    "已用"起始即非零（发送端 MANIFEST 每 32 帧轮播，中途加入先收到的全是 DATA）。
    """
    text = "中途加入流的计时回归".encode()
    sender = SendController(text, ContentType.TEXT, "", session_id=77,
                            config=SenderConfig(chunk_size_log=6, rounds=2))
    backend = StandardQrBackend()
    pipe = ReceivePipeline(backend=backend)
    # 按协议序首帧是 MANIFEST；跳过它，先喂 DATA（模拟中途加入流）
    from qrferry.core.frame import FrameType, decode_frame
    frames = iter(sender)
    manifest_raw = next(frames)
    assert decode_frame(manifest_raw)[0].frame_type == int(FrameType.MANIFEST)
    data_raw = next(frames)
    assert decode_frame(data_raw)[0].frame_type == int(FrameType.DATA)
    pipe.process_image(backend.encode(data_raw))
    assert pipe.first_valid_frame_ts is None      # MANIFEST 前的 DATA：未生效，不计时
    assert pipe.valid_frames == 0
    pipe.process_image(backend.encode(manifest_raw))
    assert pipe.first_valid_frame_ts is not None   # MANIFEST 生效：此刻起算
    for fb in frames:
        pipe.process_image(backend.encode(fb))
        if pipe.is_complete:
            break
    assert pipe.result is not None and pipe.result.data == text


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


def test_pipeline_counts_link_bytes_and_rate():
    """有效 DATA 帧累计 total_valid_bytes = N_DATA × block_size；均值速率/帧率 > 0。"""
    data = _rand(2000, 3)
    sender = SendController(data, ContentType.FILE, "f.bin", session_id=1,
                            config=SenderConfig(chunk_size_log=6, rounds=1))
    pipe = ReceivePipeline(backend=StandardQrBackend())
    _drive(sender, pipe)
    m = pipe.session.manifest
    block_size = 1 << m.chunk_size_log
    assert pipe.total_valid_bytes == pipe.valid_data_frames * block_size
    assert pipe.valid_data_frames > 0
    assert pipe.link_bps_avg > 0
    assert pipe.fps_avg > 0
    # 有效速率（按原始文件字节计，raw_size×进度/耗时）：> 0；随机字节不可压缩
    # raw≈encoded，且 resolved ≤ valid_data_frames，故 effective ≤ link 仍成立
    # （可压缩数据下 effective 反映原始字节、可能超过链路字节，属预期）。
    assert pipe.effective_bps > 0
    # 计数层做精确断言（每个唯一符号必对应一次有效帧递增，不受时间影响）。
    assert pipe.session.unique_symbols <= pipe.valid_data_frames
    # bps 层受 time.monotonic 两次取样亚微秒抖动影响，在二者相等时偶发 ±ppm
    # 翻转，故用相对容差同时兜住真回归（若 effective 数倍超 link 仍会失败）。
    assert pipe.link_bps_avg >= pipe.effective_bps * (1 - 1e-3)


def test_pipeline_eta_none_below_threshold():
    """unique_symbols<3 或 elapsed<1s 时 ETA 为 None。"""
    data = _rand(2000, 4)
    sender = SendController(data, ContentType.FILE, "f.bin", session_id=1,
                            config=SenderConfig(chunk_size_log=6, rounds=1))
    pipe = ReceivePipeline(backend=StandardQrBackend())
    frames = list(sender)
    from qrferry.core.frame import FrameType, decode_frame
    manifest_f = next(f for f in frames if decode_frame(f)[0].frame_type == FrameType.MANIFEST)
    pipe.process_image(pipe.backend.encode(manifest_f))
    assert pipe.eta_seconds is None   # 仅 MANIFEST：unique=0 且 elapsed≈0


def test_persist_snapshot_is_time_throttled(monkeypatch):
    """续传快照：满 16 帧才写，且两次写入间隔不小于 2 秒（避免大文件周期性卡顿）。"""
    from qrferry.app import receive_pipeline as rp

    calls = []
    monkeypatch.setattr(rp.session_store, "save", lambda *a, **k: calls.append(1))
    pipe = rp.ReceivePipeline(persist=True, save_dir=".")
    for _ in range(16):
        pipe._maybe_save(1)
    assert len(calls) == 1, "满 16 帧应落一次盘"
    for _ in range(200):
        pipe._maybe_save(1)
    assert len(calls) == 1, "2 秒冷却内不得重复落盘"
    # 冷却过后（伪造时间基准）应再次落盘
    pipe._last_save_ts -= rp._PERSIST_MIN_SECONDS + 1
    for _ in range(16):
        pipe._maybe_save(1)
    assert len(calls) == 2
