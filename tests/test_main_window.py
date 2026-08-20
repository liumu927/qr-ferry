"""主窗口 smoke 测试（offscreen，验证可构建、控件齐全、模式切换不崩溃）。"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from qrferry.app.main_window import MainWindow, _build_style
from qrferry.core.frame import ContentType


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _stub_camera_probe(monkeypatch):
    """主窗口构建即触发后台摄像头探测；测试环境打桩为空，避免真实打开设备。

    两个入口都要打桩：刷新优先走子进程探测（probe_available_cameras_fresh），
    桩返回 None 表示「子进程不可用」，自动回退进程内探测的桩。
    """
    from qrferry.app import main_window
    monkeypatch.setattr(main_window, "probe_available_cameras", lambda max_index=9: [])
    monkeypatch.setattr(
        main_window, "probe_available_cameras_fresh", lambda max_index=9: None)


def test_window_constructs(qapp):
    w = MainWindow()
    assert w.tabs.count() == 2
    assert w._qr_label is not None
    assert w._cam_label is not None
    assert w._r_progress.value() == 0
    assert not hasattr(w, "_r_fps")
    assert not hasattr(w, "_r_dir")
    assert not hasattr(w, "_r_pick")
    assert w._r_save.isHidden()


def test_toggle_maximize_manual_flag_and_restore(qapp):
    """无边框窗口最大化/还原走手动几何实现：标志位翻转、还原恢复原几何与按钮提示。

    回归：Qt 6.8/Windows 无边框窗口 showMaximized() 会落到 WindowFullScreen
    （isMaximized() 恒 False），旧实现导致「最大化后要连点两次才还原」。
    """
    w = MainWindow()
    assert w._maximized is False
    assert w._win_max.toolTip() == "最大化"
    normal = w.geometry()
    w._toggle_maximize()
    assert w._maximized is True
    assert w._win_max.toolTip() == "还原"
    w._toggle_maximize()
    assert w._maximized is False
    assert w.geometry() == normal
    assert w._win_max.toolTip() == "最大化"


def test_receive_uses_internal_poll_rate(qapp, monkeypatch):
    """接收端采集在独立后台线程持续运行，不依赖用户输入帧率。"""
    import numpy as np

    from qrferry.app import main_window

    class FakeCamera:
        def isOpened(self):
            return True

        def set(self, *_args):
            return True

        def read(self):
            return True, np.full((480, 640, 3), 120, dtype=np.uint8)

        def release(self):
            pass

    w = MainWindow()
    monkeypatch.setattr(main_window, "open_camera", lambda _index, backend="auto": FakeCamera())
    monkeypatch.setattr(w, "_probe_resume", lambda: None)

    w._start_recv()
    assert w._capture_thread is not None and w._capture_thread.is_alive()
    w._stop_recv()
    assert w._capture_thread is None


def _fake_devices():
    from qrferry.app.camera_devices import CameraDevice
    return [CameraDevice(0, "CamA", True), CameraDevice(1, "CamB", True)]


def test_probe_done_defaults_to_first_device(qapp):
    """探测完成默认选中第一个可用设备。"""
    w = MainWindow()
    w._on_camera_probe_done(_fake_devices())
    assert w._r_cam.currentIndex() == 0
    assert w._r_cam.currentData() == 0


def test_probe_done_restores_tracked_selection(qapp):
    """用户选过设备后刷新：恢复原选择而非重置回第一项。"""
    w = MainWindow()
    w._cam_selected_idx = 1
    w._on_camera_probe_done(_fake_devices())
    assert w._r_cam.currentIndex() == 1
    assert w._r_cam.currentData() == 1


def test_probe_done_falls_back_when_tracked_device_gone(qapp):
    """原选择设备已不可用时回退第一项，不残留失效索引。"""
    w = MainWindow()
    w._cam_selected_idx = 5
    w._on_camera_probe_done(_fake_devices())
    assert w._r_cam.currentIndex() == 0


def test_probe_worker_emits_empty_on_exception(qapp, monkeypatch):
    """探测线程异常也必须落兜底项并恢复按钮，避免卡在「正在探测摄像头…」。"""
    from qrferry.app import main_window

    w = MainWindow()
    monkeypatch.setattr(
        main_window, "probe_available_cameras",
        lambda max_index=9: (_ for _ in ()).throw(RuntimeError("probe boom")))
    w._camera_probe_worker()   # 同线程 emit → 同步回调
    assert w._r_cam.count() == 1
    assert w._r_cam.currentData() == 0
    assert w._r_start.isEnabled()


def test_probe_worker_prefers_fresh_subproc_result(qapp, monkeypatch):
    """子进程探测成功时直接采用其结果，不再跑进程内探测（热插拔设备经子进程可见）。"""
    from qrferry.app import main_window
    from qrferry.app.camera_devices import CameraDevice

    w = MainWindow()

    def _boom(max_index=9):
        raise AssertionError("子进程成功时不应回退进程内探测")

    monkeypatch.setattr(main_window, "probe_available_cameras", _boom)
    monkeypatch.setattr(
        main_window, "probe_available_cameras_fresh",
        lambda max_index=9: [CameraDevice(0, "热插拔摄像头", True, backend="dshow")])
    w._camera_probe_worker()
    assert w._r_cam.count() == 1
    assert w._r_cam.currentData() == 0
    assert "热插拔摄像头" in w._r_cam.currentText()


def test_capture_loop_retries_open(qapp, monkeypatch):
    """打开摄像头首次失败（驱动/后端起手竞态）→ 自动重试成功后进入采集，不弹错误。"""
    import time as _t

    import numpy as np

    from qrferry.app import main_window

    calls = {"n": 0}
    errors = []

    class FlakyOpen:
        def isOpened(self):
            return False

        def release(self):
            pass

    class FakeCamera:
        def isOpened(self):
            return True

        def set(self, *_a):
            return True

        def read(self):
            return True, np.full((480, 640, 3), 120, dtype=np.uint8)

        def release(self):
            pass

    def flaky_open(_idx, backend="auto"):
        calls["n"] += 1
        return FakeCamera() if calls["n"] >= 3 else FlakyOpen()

    w = MainWindow()
    monkeypatch.setattr(main_window, "open_camera", flaky_open)
    monkeypatch.setattr(w, "_probe_resume", lambda: None)
    w._decode_error.disconnect()                      # 防失败时弹模态框挂住测试
    w._decode_error.connect(lambda m: errors.append(m))

    w._start_recv()
    deadline = _t.monotonic() + 3.0
    while w._cap is None and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert w._cap is not None, "重试后应成功打开摄像头"
    assert calls["n"] >= 2, "应发生至少一次重试"
    assert errors == [], f"不应报错，实际: {errors}"
    w._stop_recv()


def test_capture_loop_retries_when_open_but_read_fails(qapp, monkeypatch):
    """isOpened 成功但 read 不出帧（MSMF 假可用）→ 按 read 判据重试成功后进入采集。"""
    import time as _t

    import numpy as np

    from qrferry.app import main_window

    calls = {"n": 0}
    errors = []

    class NoFrameCap:
        def isOpened(self):
            return True

        def read(self):
            return False, None

        def set(self, *_a):
            return True

        def release(self):
            pass

    class FakeCamera:
        def isOpened(self):
            return True

        def set(self, *_a):
            return True

        def read(self):
            return True, np.full((480, 640, 3), 120, dtype=np.uint8)

        def release(self):
            pass

    def flaky_open(_idx, backend="auto"):
        calls["n"] += 1
        return FakeCamera() if calls["n"] >= 3 else NoFrameCap()

    w = MainWindow()
    monkeypatch.setattr(main_window, "open_camera", flaky_open)
    monkeypatch.setattr(w, "_probe_resume", lambda: None)
    w._decode_error.disconnect()
    w._decode_error.connect(lambda m: errors.append(m))

    w._start_recv()
    deadline = _t.monotonic() + 5.0   # 打开探测含帧间间隔（热启动容忍），给足余量
    while w._cap is None and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert w._cap is not None, "read 判据重试后应成功进入采集"
    assert calls["n"] >= 2, "应发生至少一次重试"
    assert errors == [], f"不应报错，实际: {errors}"
    w._stop_recv()


def test_capture_loop_advances_past_black_camera(qapp, monkeypatch):
    """虚拟摄像头空载输出全黑帧 → 自动顺延到下一个有画面的设备并同步下拉框。"""
    import numpy as np
    from PySide6.QtCore import QEventLoop, QTimer

    from qrferry.app import main_window
    from qrferry.app.camera_devices import CameraDevice

    errors = []

    class BlackCam:
        """能打开但只出纯黑帧（虚拟摄像头空载的典型表现）。"""
        def isOpened(self):
            return True

        def read(self):
            return True, np.zeros((480, 640, 3), dtype=np.uint8)

        def set(self, *_a):
            return True

        def release(self):
            pass

    class RealCam:
        def isOpened(self):
            return True

        def set(self, *_a):
            return True

        def read(self):
            return True, np.full((480, 640, 3), 120, dtype=np.uint8)

        def release(self):
            pass

    # 禁用构造期探测线程：其 queued done（桩返回空）会在后续 loop.exec 时被派发，
    # 把下拉框重置回兜底项——本测试只用手动 populate，保证确定性。
    monkeypatch.setattr(main_window.MainWindow, "_refresh_camera_devices", lambda self: None)
    w = MainWindow()
    w._on_camera_probe_done([CameraDevice(0, "虚拟摄像头", True),
                             CameraDevice(1, "物理摄像头", True)])
    assert w._r_cam.currentData() == 0          # 默认选中第一个（黑屏的虚拟摄像头）
    monkeypatch.setattr(main_window, "open_camera",
                        lambda idx, backend="auto": BlackCam() if idx == 0 else RealCam())
    monkeypatch.setattr(w, "_probe_resume", lambda: None)
    w._decode_error.disconnect()
    w._decode_error.connect(lambda m: errors.append(m))

    try:
        # 用事件循环等 _device_in_use（顺延成功的同步点），避免忙轮询 processEvents
        # 被采集线程的帧事件洪流拖死。
        loop = QEventLoop()
        used = []
        w._device_in_use.connect(lambda i: (used.append(i), loop.quit()))
        QTimer.singleShot(8000, loop.quit)
        w._start_recv()
        loop.exec()
        assert used == [1], f"应顺延到设备 1，实际: {used}"
        assert w._cap is not None, "顺延后应成功进入采集"
        assert errors == [], f"不应报错，实际: {errors}"
        assert w._r_cam.currentData() == 1, "下拉框应同步到实际使用的设备"
    finally:
        w._stop_recv()


def test_capture_loop_opens_with_probed_backend(qapp, monkeypatch):
    """热插拔设备按探测记录的后端（dshow）打开，而不是只试默认后端。"""
    import time as _t

    import numpy as np

    from qrferry.app import main_window
    from qrferry.app.camera_devices import CameraDevice

    calls = []

    class FakeCam:
        def isOpened(self):
            return True

        def set(self, *_a):
            return True

        def read(self):
            return True, np.full((480, 640, 3), 120, dtype=np.uint8)

        def release(self):
            pass

    def recording_open(idx, backend="auto"):
        calls.append((idx, backend))
        return FakeCam()

    monkeypatch.setattr(main_window.MainWindow, "_refresh_camera_devices", lambda self: None)
    w = MainWindow()
    w._on_camera_probe_done([CameraDevice(0, "热插拔摄像头", True, backend="dshow")])
    monkeypatch.setattr(main_window, "open_camera", recording_open)
    monkeypatch.setattr(w, "_probe_resume", lambda: None)
    w._decode_error.disconnect()
    w._decode_error.connect(lambda m: None)

    w._start_recv()
    deadline = _t.monotonic() + 5.0
    while w._cap is None and _t.monotonic() < deadline:
        _t.sleep(0.05)
    assert calls, "应发生打开调用"
    assert calls[0] == (0, "dshow"), f"应先按探测后端 dshow 打开，实际: {calls[:1]}"
    assert w._cap is not None, "按探测后端打开后应进入采集"
    w._stop_recv()


def test_capture_loop_falls_back_to_auto_when_probed_backend_fails(qapp, monkeypatch):
    """探测后端打开失败（设备插拔后索引漂移/驱动重协商）→ 回退 auto 重试成功。"""
    import time as _t

    import numpy as np

    from qrferry.app import main_window
    from qrferry.app.camera_devices import CameraDevice

    calls = []

    class DeadDshow:
        """dshow 能打开但永不出帧（陈旧项）。"""
        def isOpened(self):
            return True

        def read(self):
            return False, None

        def set(self, *_a):
            return True

        def release(self):
            pass

    class FakeCam:
        def isOpened(self):
            return True

        def set(self, *_a):
            return True

        def read(self):
            return True, np.full((480, 640, 3), 120, dtype=np.uint8)

        def release(self):
            pass

    def flaky_open(idx, backend="auto"):
        calls.append((idx, backend))
        return DeadDshow() if backend == "dshow" else FakeCam()

    monkeypatch.setattr(main_window.MainWindow, "_refresh_camera_devices", lambda self: None)
    w = MainWindow()
    w._on_camera_probe_done([CameraDevice(0, "热插拔摄像头", True, backend="dshow")])
    monkeypatch.setattr(main_window, "open_camera", flaky_open)
    monkeypatch.setattr(w, "_probe_resume", lambda: None)
    w._decode_error.disconnect()
    w._decode_error.connect(lambda m: None)

    w._start_recv()
    deadline = _t.monotonic() + 8.0
    while w._cap is None and _t.monotonic() < deadline:
        _t.sleep(0.05)
    used_backends = [b for _, b in calls]
    assert "dshow" in used_backends, "应先尝试探测后端 dshow"
    assert "auto" in used_backends, "dshow 失效后应回退 auto 重试"
    assert w._cap is not None, "回退 auto 后应成功进入采集"
    w._stop_recv()


def test_capture_loop_falls_back_when_stream_goes_black_after_open(qapp, monkeypatch):
    """打开阶段有画面、进入采集后持续黑帧（切流/协商后黑屏）→ 宽限期检测后顺延。"""
    import time as _t

    import numpy as np
    from PySide6.QtCore import QEventLoop, QTimer

    from qrferry.app import main_window
    from qrferry.app.camera_devices import CameraDevice

    errors = []

    class BlackAfterOpenCam:
        """打开探测时头一帧有画面（能通过打开判据），随后持续纯黑——覆盖
        「打开正常、设 MJPG/720p/60fps 后流变黑」这类上一轮修不到的场景。"""
        def __init__(self):
            self.n = 0

        def isOpened(self):
            return True

        def set(self, *_a):
            return True

        def read(self):
            self.n += 1
            if self.n == 1:
                return True, np.full((480, 640, 3), 120, dtype=np.uint8)
            return True, np.zeros((480, 640, 3), dtype=np.uint8)

        def release(self):
            pass

    class RealCam:
        def isOpened(self):
            return True

        def set(self, *_a):
            return True

        def read(self):
            return True, np.full((480, 640, 3), 120, dtype=np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(main_window.MainWindow, "_refresh_camera_devices", lambda self: None)
    w = MainWindow()
    w._on_camera_probe_done([CameraDevice(0, "虚拟摄像头", True),
                             CameraDevice(1, "物理摄像头", True)])
    monkeypatch.setattr(main_window, "open_camera",
                        lambda idx, backend="auto": BlackAfterOpenCam() if idx == 0 else RealCam())
    monkeypatch.setattr(w, "_probe_resume", lambda: None)
    w._decode_error.disconnect()
    w._decode_error.connect(lambda m: errors.append(m))

    try:
        loop = QEventLoop()
        used = []
        w._device_in_use.connect(lambda i: (used.append(i), loop.quit()))
        QTimer.singleShot(12000, loop.quit)
        t0 = _t.monotonic()
        w._start_recv()
        loop.exec()
        elapsed = _t.monotonic() - t0
        assert used == [1], f"打开后黑屏应顺延到设备 1，实际: {used}"
        assert w._cap is not None, "顺延后应成功进入采集"
        assert errors == [], f"不应报错，实际: {errors}"
        assert w._r_cam.currentData() == 1, "下拉框应同步到实际使用的设备"
        assert elapsed >= 2.5, (
            f"黑屏检测应等待连续黑帧窗口（3s）而非立即顺延，实际 {elapsed:.1f}s")
    finally:
        w._stop_recv()


def test_is_black_frame_catches_noisy_black(qapp):
    """带噪点的黑帧也应判为「无画面」：旧判据（均值+标准差）会被噪点抬高标准差
    而漏判，导致黑屏虚拟摄像头被当成可用设备。"""
    import numpy as np

    from qrferry.app.main_window import _is_black_frame

    # 均匀噪点黑帧：均值≈14(<15)、max≈27(<40)、标准差≈8.4（旧判据会漏判）
    noisy_black = np.random.default_rng(0).integers(0, 28, size=(480, 640, 3), dtype=np.uint8)
    assert _is_black_frame(noisy_black)

    # 暗场景但存在亮斑（灯/反光）：max≥40 → 视为有画面，不得误杀
    dark_scene = np.full((480, 640, 3), 8, dtype=np.uint8)
    dark_scene[100:110, 200:210] = 180
    assert not _is_black_frame(dark_scene)

    # 正常亮度帧：有画面
    assert not _is_black_frame(np.full((480, 640, 3), 120, dtype=np.uint8))


def test_decode_progress_bar_matches_status_bar(qapp):
    """进度条与左下角状态栏同取 pipe.progress（唯一权威来源），round 与 :.0% 同口径，
    且不再走平滑动画（动画会让进度条滞后于真实值，两处显示不一致）。"""
    w = MainWindow()
    manifest = type("M", (), {"raw_size": 1000, "total_chunks": 5})()
    session = type("S", (), {"manifest": manifest})()
    w._pipe = type("P", (), {
        "session": session, "result": None, "progress": 0.999,
        "missing_indices": [], "is_complete": False,
        "valid_data_frames": 50, "bad_frames": 0, "drop_rate": 0.0,
        "missed_images": 0, "elapsed_seconds": 10.0,
        "link_bps_instant": 0.0, "link_bps_avg": 0.0,
        "fps_instant": 0.0, "eta_seconds": None, "effective_bps": 0.0,
    })()
    w._recv_start_ts = 0.0
    w._capture_thread = object()   # 通过「接收中」守卫，不实际起线程
    w._r_progress.setValue(0)
    w._on_decode_progress()
    assert w._r_progress.value() == 100          # round(0.999*100)=100，与 :.0% 同口径
    assert "进度 100%" in w.statusBar().currentMessage()
    assert not hasattr(w, "_prog_anim"), "不再使用平滑动画，进度条直接反映真实值"


@pytest.mark.parametrize(
    ("dark", "background", "foreground"),
    [(False, "#FFFFFF", "#0F172A"), (True, "#1E293B", "#E2E8F0")],
)
def test_message_box_text_contrasts_with_background(dark, background, foreground):
    """提示弹窗应显式使用当前主题的高对比度背景和文字色。"""
    style = _build_style(dark)
    assert f"QMessageBox {{ background: {background}; color: {foreground}; }}" in style
    assert f"QMessageBox QLabel {{ color: {foreground}; background: transparent; }}" in style


def test_send_mode_switch(qapp):
    w = MainWindow()
    assert w._s_file.isChecked()
    w._s_text.setChecked(True)
    assert w._s_text.isChecked()
    w._s_file.setChecked(True)
    assert w._s_file.isChecked()


def test_compose_grid(qapp):
    from PIL import Image
    w = MainWindow()
    w._send_ctrl = type("C", (), {"config": type("K", (), {"grid": (2, 2)})()})()
    imgs = [Image.new("L", (40, 40), 255) for _ in range(4)]
    canvas = w._compose_grid(imgs)
    assert canvas.size == (80, 80)


def test_receive_count_during_transfer_shows_block_progress(qapp):
    """传输中（已收 manifest、未完成）：文本尚未解码，角标显示块恢复进度。"""
    w = MainWindow()
    manifest = type("M", (), {"total_chunks": 5})()
    session = type("S", (), {"manifest": manifest})()
    w._pipe = type("P", (), {"session": session, "result": None, "progress": 0.4})()
    w._update_r_count()
    assert w._r_count.text() == "2/5 块"


def test_receive_count_text_complete_uses_chars_not_bytes(qapp):
    """文本接收完成：角标显示字符数（与发送端口径一致），不得用 UTF-8 字节数。"""
    w = MainWindow()
    result = type("R", (), {
        "content_type": ContentType.TEXT,
        "data": "你好ABC".encode(),
    })()
    session = type("S", (), {"manifest": None})()
    w._pipe = type("P", (), {"session": session, "result": result})
    w._r_result.setPlainText("你好ABC")   # 5 字符 / UTF-8 9 字节
    w._update_r_count()
    assert w._r_count.text() == "5 字符"


def test_receive_missing_shown_when_blocks_missing(qapp):
    """传输中存在未恢复块：缺块明细可见并展示索引。"""
    w = MainWindow()
    session = type("S", (), {"manifest": type("M", (), {"total_chunks": 10})()})()
    w._pipe = type("P", (), {
        "session": session, "result": None, "progress": 0.2,
        "missing_indices": [3, 7], "is_complete": False,
    })()
    w._update_r_missing()
    assert w._r_missing.text() == "缺 2 块: [3, 7]"
    assert not w._r_missing.isHidden()


def test_receive_missing_hidden_when_no_missing(qapp):
    """无缺块（未开始或已完成）：明细隐藏且文本清空。"""
    w = MainWindow()
    session = type("S", (), {"manifest": None})()
    w._pipe = type("P", (), {
        "session": session, "result": None, "progress": 0.0,
        "missing_indices": [], "is_complete": False,
    })()
    w._update_r_missing()
    assert w._r_missing.isHidden()
    assert w._r_missing.text() == ""


def test_receive_missing_truncates_long_list(qapp):
    """缺块超过 20 个：仅显示前 20 个索引并追加省略号。"""
    w = MainWindow()
    session = type("S", (), {"manifest": type("M", (), {"total_chunks": 100})()})()
    w._pipe = type("P", (), {
        "session": session, "result": None, "progress": 0.0,
        "missing_indices": list(range(25)), "is_complete": False,
    })()
    w._update_r_missing()
    text = w._r_missing.text()
    assert text.startswith("缺 25 块: [")
    assert text.endswith("…")          # 超过 20 个时以省略号结尾
    assert "20" not in text            # 第 21 个及之后的索引不展示


def test_resend_without_session_warns(qapp, monkeypatch):
    """无活跃发送会话时点补发应提示，而非崩溃或启动定时器。"""
    from PySide6.QtWidgets import QMessageBox
    w = MainWindow()
    w._send_ctrl = None
    called = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: called.append(a))
    w._resend()
    assert called   # 弹了提示，未进入补发分支


def test_receive_stats_displays_valid_and_dropped(qapp):
    """传输中统计行展示有效/丢弃/丢弃率。"""
    w = MainWindow()
    manifest = type("M", (), {"raw_size": 1000, "total_chunks": 5})()
    session = type("S", (), {"manifest": manifest})()
    w._pipe = type("P", (), {
        "session": session, "result": None, "progress": 0.4,
        "missing_indices": [], "is_complete": False,
        "valid_data_frames": 50, "bad_frames": 5, "drop_rate": 5 / 55,
        "missed_images": 12,
        "elapsed_seconds": 10.0,
        "link_bps_instant": 0.0, "link_bps_avg": 0.0,
        "fps_instant": 0.0, "eta_seconds": None,
        "effective_bps": 0.0,
    })()
    w._recv_start_ts = 0.0
    w._update_r_stats()
    text = w._r_stats_left.text()
    assert "已收 50" in text
    assert "丢弃 5" in text
    assert "9.1%" in text            # 5/55 ≈ 9.09% → :.1% = "9.1%"
    assert "未识别 12" in text
    assert not w._r_stats.isHidden()


def test_receive_stats_no_second_rate_when_complete(qapp):
    """完成态不追加第二个 KB/s 速率：有效=去重搬运速率自洽（与 ETA/进度同口径），
    避免与基于原始文件字节的端到端速率并列造成「哪个才是真速度」的误导。"""
    w = MainWindow()
    manifest = type("M", (), {"raw_size": 2048})()
    session = type("S", (), {"manifest": manifest})()
    result = type("R", (), {"data": b"x" * 2048, "content_type": ContentType.FILE})()
    w._pipe = type("P", (), {
        "session": session, "result": result, "progress": 1.0,
        "missing_indices": [], "is_complete": True,
        "valid_data_frames": 100, "bad_frames": 0, "drop_rate": 0.0,
        "missed_images": 0,
        "elapsed_seconds": 2.0,
        "link_bps_instant": 0.0, "link_bps_avg": 0.0,
        "fps_instant": 0.0, "eta_seconds": None,
        "effective_bps": 1024.0,
    })()
    w._recv_start_ts = 0.0
    w._update_r_stats()
    assert "已收 100" in w._r_stats_left.text()
    assert "完成" not in w._r_stats_right.text()      # 不再出现第二个速率
    assert w._r_stats_right.text().count("KB/s") == 1  # 仅保留有效一个速率


def test_received_file_waits_for_user_save(qapp):
    """文件完成后仅展示保存动作，不提前写入或声称已保存。"""
    w = MainWindow()
    result = type("R", (), {
        "content_type": ContentType.FILE,
        "filename": "报告.pdf",
        "data": b"pdf-data",
        "compression": 2,          # ZSTD
        "raw_size": 10000,
        "encoded_size": 4000,
    })()
    session = type("S", (), {"manifest": None})()
    w._pipe = type("P", (), {"result": result, "session": session})()

    w._on_received()

    assert not w._r_save.isHidden()
    assert w._r_copy.isHidden()
    assert "文件接收完成：报告.pdf" in w._r_result.toPlainText()
    assert "已保存" not in w._r_result.toPlainText()
    # 压缩信息随结果展示：算法 + 原始/传输字节 + 省幅
    assert "压缩: ZSTD 10000→4000 字节" in w._r_result.toPlainText()


def test_save_received_file_uses_sender_name_and_extension(qapp, monkeypatch, tmp_path):
    """保存弹窗默认沿用发送端文件名和类型，并允许用户自定义路径。"""
    from PySide6.QtWidgets import QFileDialog

    w = MainWindow()
    result = type("R", (), {
        "content_type": ContentType.FILE,
        "filename": "报告.pdf",
        "data": b"pdf-data",
    })()
    w._pipe = type("P", (), {"result": result})()
    captured = {}

    def choose_path(_parent, _title, default_path, file_filter):
        captured["default_path"] = default_path
        captured["file_filter"] = file_filter
        return str(tmp_path / "自定义报告"), "PDF 文件 (*.pdf)"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_path)
    w._save_received_file()

    saved = tmp_path / "自定义报告.pdf"
    assert saved.read_bytes() == b"pdf-data"
    assert os.path.basename(captured["default_path"]) == "报告.pdf"
    assert captured["file_filter"].startswith("PDF 文件 (*.pdf)")
    assert str(saved) in w._r_result.toPlainText()


def _photo_png_file(tmp_path) -> str:
    """生成照片类 PNG（高斯噪声渐变，JPEG 转换收益大）并返回路径。"""
    import random

    from PIL import Image

    rng = random.Random(1)
    img = Image.new("RGB", (800, 600))
    px = img.load()
    for y in range(600):
        for x in range(800):
            px[x, y] = (
                min(255, int(255 * x / 800 + rng.gauss(0, 20))),
                min(255, int(255 * y / 600 + rng.gauss(0, 20))),
                min(255, int((255 * x / 800 + 255 * y / 600) / 2 + rng.gauss(0, 20))),
            )
    path = str(tmp_path / "photo.png")
    img.save(path, "PNG")
    return path


def test_optimize_preview_shows_conversion(qapp, monkeypatch, tmp_path):
    """勾选「优化文件」后文件标签即时预览 原文件 → 优化结果（照片 PNG → JPG）。"""
    w = MainWindow()
    w._file_path = _photo_png_file(tmp_path)
    w._s_optimize.setChecked(True)   # 触发 _update_optimize_preview
    assert "→" in w._s_filelabel.text()
    assert "photo.jpg" in w._s_filelabel.text()


def test_start_send_uses_optimized_bytes_when_checked(qapp, monkeypatch, tmp_path):
    """勾选「优化文件」发送照片 PNG：SendController 收到优化后字节（JPEG）与改名 filename。"""
    import qrferry.app.session_store as ss

    monkeypatch.setattr(ss, "save_sender", lambda *a, **k: None)   # 不写 ~/.qrferry
    w = MainWindow()
    w._file_path = _photo_png_file(tmp_path)
    w._s_optimize.setChecked(True)
    w._start_send()
    c = w._send_ctrl
    assert c._optimized is True
    assert c.filename == "photo.jpg"
    assert c.raw_data[:2] == b"\xff\xd8"          # JPEG magic：发送的是优化后字节
    assert c.raw_data[:2] != b"\x89P"             # 不是原 PNG
