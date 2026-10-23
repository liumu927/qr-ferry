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


# ────────────── 线程容错：单帧异常不杀线程 + 看门狗兜底 ──────────────
# 回归背景：_decode_loop 的 cvtColor 在 try 外、try 内只接 ValueError，
# _is_black_frame 无条件 cvtColor，采集线程无兜底。任一异常都让线程静默死亡，
# 而 UI 仍显示「接收中」、预览照刷、进度不动，即「偶发长时间超时无输出」。


def test_is_black_frame_accepts_gray_and_bgra(qapp):
    """灰度(2D)/单通道 3D/BGRA 帧不得抛 cv2.error（旧实现无条件 BGR2GRAY）。"""
    import numpy as np

    from qrferry.app.main_window import _is_black_frame

    assert _is_black_frame(np.zeros((8, 8), dtype=np.uint8)) is True
    assert _is_black_frame(np.zeros((8, 8, 1), dtype=np.uint8)) is True
    assert _is_black_frame(np.zeros((8, 8, 4), dtype=np.uint8)) is True
    assert _is_black_frame(np.full((8, 8), 200, dtype=np.uint8)) is False
    assert _is_black_frame(np.full((8, 8, 4), 200, dtype=np.uint8)) is False


def _run_decode_loop(w, pipe, frames, wait_until=None, settle=1.0):
    """起解码线程、喂帧、等条件满足（或线程退出）、再停机；返回 (线程是否存活, 错误列表)。"""
    import queue as _q
    import threading as _th
    import time as _t

    errors = []
    w._pipe = pipe
    w._decode_queue = _q.Queue()
    w._recv_generation = 7
    w._decode_stop.clear()
    w._decode_error.disconnect()
    w._decode_error.connect(lambda m: errors.append(m))
    t = _th.Thread(target=w._decode_loop, args=(w._decode_queue, 7), daemon=True)
    t.start()
    for f in frames:
        w._decode_queue.put(f)
        _t.sleep(0.03)
    deadline = _t.monotonic() + settle
    while _t.monotonic() < deadline:
        if errors or not t.is_alive():
            break
        if wait_until is not None and wait_until():
            break
        _t.sleep(0.02)
    _t.sleep(0.05)
    # 信号槽对 lambda 走队列投递：断言前必须把主线程事件循环跑一遍
    QApplication.processEvents()
    alive = t.is_alive()
    w._decode_stop.set()
    t.join(1.0)
    QApplication.processEvents()
    return alive, errors


def test_decode_loop_survives_transient_frame_error(qapp):
    """单帧抛 OSError（存档落盘失败等）只跳过，后续帧继续解码，线程不退出。"""
    import numpy as np

    class FlakyPipe:
        backend = object()
        progress = 0.0
        result = None

        def __init__(self):
            self.calls = 0

        def process_image(self, image):
            self.calls += 1
            if self.calls == 2:
                raise OSError("snapshot disk full")

    pipe = FlakyPipe()
    w = MainWindow()
    alive, errors = _run_decode_loop(
        w, pipe, [np.zeros((4, 4, 3), np.uint8)] * 3, wait_until=lambda: pipe.calls >= 3)
    assert pipe.calls == 3, "异常帧之后的帧仍应被处理"
    assert alive is True, "单帧异常不得杀死解码线程"
    assert errors == [], f"单帧异常不应报错停机: {errors}"


def test_decode_loop_normalizes_bgra_frame(qapp):
    """BGRA(4 通道) 帧统一转 BGR 后正常解码，不再因 cvtColor 抛错而丢帧/杀线程。"""
    import numpy as np

    class CountPipe:
        backend = object()
        progress = 0.0
        result = None

        def __init__(self):
            self.shapes = []

        def process_image(self, image):
            self.shapes.append(image.shape)

    pipe = CountPipe()
    w = MainWindow()
    frames = [np.zeros((8, 8, 4), np.uint8), np.zeros((8, 8, 3), np.uint8)]
    alive, errors = _run_decode_loop(
        w, pipe, frames, wait_until=lambda: len(pipe.shapes) >= 2)
    assert alive is True
    assert errors == []
    assert pipe.shapes == [(8, 8), (8, 8)], "4 通道帧应被转换后正常处理"


def test_decode_loop_skips_unconvertible_frame_and_continues(qapp):
    """真正无法转换的帧（如 2 通道）只跳过，下一帧仍正常处理，线程不退出。"""
    import numpy as np

    class CountPipe:
        backend = object()
        progress = 0.0
        result = None

        def __init__(self):
            self.shapes = []

        def process_image(self, image):
            self.shapes.append(image.shape)

    pipe = CountPipe()
    w = MainWindow()
    frames = [np.zeros((8, 8, 2), np.uint8), np.zeros((8, 8, 3), np.uint8)]
    alive, errors = _run_decode_loop(
        w, pipe, frames, wait_until=lambda: len(pipe.shapes) >= 1)
    assert alive is True
    assert errors == []
    assert pipe.shapes == [(8, 8)], "坏帧应被跳过，只处理可转换的帧"


def test_decode_loop_stops_on_session_corrupt(qapp):
    """SHA-256 校验失败属会话级损坏：停机报错，不按单帧错误继续。"""
    import numpy as np

    from qrferry.app.receive_pipeline import SessionCorruptError

    class CorruptPipe:
        backend = object()
        progress = 0.0
        result = None

        def __init__(self):
            self.calls = 0

        def process_image(self, image):
            self.calls += 1
            raise SessionCorruptError("SHA-256 校验失败：数据损坏")

    pipe = CorruptPipe()
    w = MainWindow()
    alive, errors = _run_decode_loop(w, pipe, [np.zeros((4, 4, 3), np.uint8)] * 3)
    assert alive is False, "会话级损坏应停机"
    assert pipe.calls == 1, "停机后不应继续处理后续帧"
    assert errors and "SHA-256" in errors[0]


def test_watchdog_restarts_dead_decode_thread(qapp):
    """解码线程异常死亡后，看门狗把它拉起来（会话状态在 UI 侧，重启无副作用）。"""
    import queue as _q
    import threading as _th

    w = MainWindow()
    w._decode_error.disconnect()
    errors = []
    w._decode_error.connect(lambda m: errors.append(m))
    w._r_start.blockSignals(True)
    w._r_start.setChecked(True)     # 只翻转状态，不触发 _start_recv（不碰真实摄像头）
    w._r_start.blockSignals(False)
    w._pipe = object()              # 非 None 即可，看门狗只判活
    w._decode_queue = _q.Queue()
    w._recv_generation = 3
    w._decode_stop.clear()
    w._decode_restarts = 0
    w._last_decode_restart = 0.0
    dead = _th.Thread(target=lambda: None)
    dead.start()
    dead.join()
    w._decode_thread = dead

    w._check_recv_threads()
    assert w._decode_thread is not dead
    assert w._decode_thread.is_alive()
    assert errors == []
    w._decode_stop.set()
    w._decode_thread.join(1.0)


def test_watchdog_stops_when_decode_thread_keeps_dying(qapp):
    """短时间内反复死亡 → 不再重启，停机报错。"""
    import queue as _q
    import threading as _th
    import time as _t

    w = MainWindow()
    w._decode_error.disconnect()
    errors = []
    w._decode_error.connect(lambda m: errors.append(m))
    w._r_start.blockSignals(True)
    w._r_start.setChecked(True)
    w._r_start.blockSignals(False)
    w._pipe = object()
    w._decode_queue = _q.Queue()
    w._recv_generation = 3
    w._decode_stop.clear()
    w._decode_restarts = 1
    w._last_decode_restart = _t.monotonic()   # 刚重启过，还在冷却窗口内
    dead = _th.Thread(target=lambda: None)
    dead.start()
    dead.join()
    w._decode_thread = dead

    w._check_recv_threads()
    QApplication.processEvents()
    assert w._decode_thread is dead, "冷却窗口内不得再次重启"
    assert errors and "反复异常退出" in errors[0]


def test_capture_loop_retries_once_after_unexpected_exception(qapp, monkeypatch):
    """采集循环抛出未捕获异常时重试一次，线程不静默死亡。"""
    w = MainWindow()
    w._decode_error.disconnect()
    errors = []
    w._decode_error.connect(lambda m: errors.append(m))
    w._recv_generation = 5
    w._capture_stop.clear()
    calls = {"n": 0}

    def boom(_candidates, _gen):
        calls["n"] += 1
        raise RuntimeError("driver blew up")

    monkeypatch.setattr(w, "_capture_loop_once", boom)
    w._capture_loop([(0, "auto")], 5)
    QApplication.processEvents()
    assert calls["n"] == 2, "应重试恰好一次"
    assert errors and "采集线程异常" in errors[0]


def test_decode_loop_completes_session_despite_bad_frame(qapp):
    """端到端：真实 ReceivePipeline 中间有一帧抛 OSError（如快照落盘失败），会话仍收齐完成。"""
    import queue as _q
    import threading as _th
    import time as _t

    import numpy as np

    from qrferry.app.receive_pipeline import ReceivePipeline
    from qrferry.app.send_controller import SendController, SenderConfig
    from qrferry.core.frame import ContentType
    from qrferry.qr.backend import StandardQrBackend

    data = bytes(range(256))
    sender = SendController(data, ContentType.TEXT, "", session_id=11,
                            config=SenderConfig(chunk_size_log=6, redundancy=2.0, rounds=2))
    backend = StandardQrBackend()
    frames = [np.array(backend.encode(fb)) for fb in sender]

    class FlakyBackend:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def encode(self, *a, **k):
            return self.inner.encode(*a, **k)

        def decode(self, image):
            self.calls += 1
            if self.calls == 2:
                raise OSError("snapshot write failed")
            return self.inner.decode(image)

    w = MainWindow()
    pipe = ReceivePipeline(backend=FlakyBackend(backend), persist=False)
    errors = []
    w._pipe = pipe
    w._decode_queue = _q.Queue()
    w._recv_generation = 9
    w._decode_stop.clear()
    w._decode_error.disconnect()
    w._decode_error.connect(lambda m: errors.append(m))
    t = _th.Thread(target=w._decode_loop, args=(w._decode_queue, 9), daemon=True)
    t.start()
    deadline = _t.monotonic() + 10.0
    for f in frames:
        w._decode_queue.put(f)
        _t.sleep(0.01)
        if pipe.is_complete or _t.monotonic() > deadline:
            break
    while not pipe.is_complete and _t.monotonic() < deadline:
        _t.sleep(0.02)
    assert pipe.result is not None and pipe.result.data == data, "丢一帧不应影响会话收齐"
    assert errors == []
    w._decode_stop.set()
    t.join(2.0)


# ────────────── 文件拖拽 ──────────────


def test_dropped_file_picks_first_existing_local_file(qapp, tmp_path):
    """拖拽数据解析：只认存在的本地文件；空/文本/不存在的路径一律忽略。"""
    from PySide6.QtCore import QMimeData, QUrl

    p = tmp_path / "报告.pdf"
    p.write_bytes(b"%PDF-1.4")
    from pathlib import Path

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p))])
    assert Path(MainWindow._dropped_file(mime)) == p

    assert MainWindow._dropped_file(QMimeData()) is None
    text = QMimeData()
    text.setText("hi")
    assert MainWindow._dropped_file(text) is None
    missing = QMimeData()
    missing.setUrls([QUrl.fromLocalFile(str(tmp_path / "nope.bin"))])
    assert MainWindow._dropped_file(missing) is None
    assert MainWindow._dropped_file(QMimeData()) is None


def test_drop_file_switches_to_send_file_mode(qapp, tmp_path):
    """在接收页/文本模式下拖入文件：自动切回发送页 + 文件模式并载入。"""
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent

    p = tmp_path / "报告.pdf"
    p.write_bytes(b"%PDF-1.4")
    w = MainWindow()
    w.tabs.setCurrentIndex(1)        # 先离开发送页
    w._s_text.setChecked(True)       # 先在文本模式
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p))])
    event = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    w.dropEvent(event)
    assert event.isAccepted()
    assert w.tabs.currentIndex() == 0, "拖入文件应切回发送页"
    assert w._s_file.isChecked(), "应切到文件模式"
    from pathlib import Path
    assert Path(w._file_path) == p
    assert "报告.pdf" in w._s_filelabel.fullText()


def test_drag_enter_shows_hint_and_leave_restores(qapp, tmp_path):
    """拖入时文件行显示投放提示，拖出后恢复原文件名。"""
    from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent

    p = tmp_path / "a.bin"
    p.write_bytes(b"x")
    w = MainWindow()
    w._load_send_file(str(p))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p))])
    enter = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    w.dragEnterEvent(enter)
    assert enter.isAccepted()
    assert "松开鼠标" in w._s_filelabel.fullText()
    w.dragLeaveEvent(QDragLeaveEvent())
    assert w._s_filelabel.fullText() == "a.bin"


def test_drop_ignored_while_sending(qapp, tmp_path):
    """发送中拖入文件应被忽略（会话已按旧文件建好）。"""
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent

    p = tmp_path / "b.bin"
    p.write_bytes(b"y")
    w = MainWindow()
    w._send_timer.start(1000)        # 模拟发送中
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p))])
        event = QDropEvent(QPointF(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
        w.dropEvent(event)
        assert not event.isAccepted()
        assert w._file_path == ""
    finally:
        w._send_timer.stop()


# ────────────── 帧率看门狗（1fps 状态） ──────────────
# 回归背景：驱动偶发落入「1 帧/秒」状态——read 仍返回 ok=True、只是每次精确阻塞约
# 1000ms。旧实现只统计 read 失败与黑帧，对"读到了但很慢"没有任何监控。


def test_open_usable_camera_bails_on_slow_reads(qapp, monkeypatch):
    """探测期连续慢读（1fps 状态）提前止损，不把 8 帧探测拖成 8 秒。"""
    import numpy as np

    from qrferry.app import main_window

    reads = {"n": 0}

    class SlowCam:
        def isOpened(self):
            return True

        def read(self):
            reads["n"] += 1
            # Windows monotonic 时钟粒度约 15.6ms：睡眠取 100ms、阈值 50ms，
            # 量化误差（±15.6ms）不会把"慢读"误判成"快读"
            _t.sleep(0.1)
            return True, np.full((4, 4, 3), 120, dtype=np.uint8)

        def release(self):
            pass

        def set(self, *_a):
            return True

        def get(self, *_a):
            return 0

    import time as _t

    monkeypatch.setattr(main_window, "_SLOW_READ_SECONDS", 0.05)
    monkeypatch.setattr(main_window, "_SLOW_READ_LIMIT", 3)
    monkeypatch.setattr(main_window, "_OPEN_ATTEMPTS", 1)
    monkeypatch.setattr(main_window, "_OPEN_PROBE_INTERVAL", 0.001)
    monkeypatch.setattr(main_window, "open_camera", lambda idx, backend="auto": SlowCam())
    w = MainWindow()
    assert w._open_usable_camera(0, "auto", w._recv_generation) is None
    assert reads["n"] == 3, f"连续慢读应提前止损，实际读了 {reads['n']} 帧"


def test_open_usable_camera_accepts_fast_frames(qapp, monkeypatch):
    """正常帧率设备不受慢读判据影响：首帧即视为可用并返回。"""
    import numpy as np

    from qrferry.app import main_window

    class FastCam:
        def isOpened(self):
            return True

        def read(self):
            return True, np.full((4, 4, 3), 120, dtype=np.uint8)

        def release(self):
            pass

        def set(self, *_a):
            return True

        def get(self, *_a):
            return 0

    cap = FastCam()
    monkeypatch.setattr(main_window, "open_camera", lambda idx, backend="auto": cap)
    w = MainWindow()
    assert w._open_usable_camera(0, "auto", w._recv_generation) is cap


def test_capture_reads_reopens_on_sustained_low_fps(qapp, monkeypatch):
    """实测帧率持续低于阈值 → 原地重开；重开次数用尽仍低速 → outcome="slow"。"""
    import threading as _th
    import time as _t

    import numpy as np

    from qrferry.app import main_window

    class SlowCam:
        def __init__(self):
            self.released = 0

        def read(self):
            _t.sleep(0.01)
            return True, np.full((4, 4, 3), 120, dtype=np.uint8)

        def release(self):
            self.released += 1

    monkeypatch.setattr(main_window, "_MIN_FPS", 1000.0)   # 0.01s/帧 ≈100fps，低于该阈值
    monkeypatch.setattr(main_window, "_CRITICAL_FPS", 5000.0)   # 视为极低帧率：重开无效才停
    monkeypatch.setattr(main_window, "_FPS_WARMUP_SECONDS", 0.0)
    monkeypatch.setattr(main_window, "_LOW_FPS_SECONDS", 0.05)
    monkeypatch.setattr(main_window, "_FPS_MIN_SAMPLES", 4)
    monkeypatch.setattr(main_window, "_BLACK_GRACE_SECONDS", 0.0)

    w = MainWindow()
    w._recv_generation = 3
    w._capture_stop.clear()
    w._decode_queue = None
    reopens = {"n": 0}

    def fake_reopen(_idx, _backend, _gen):
        reopens["n"] += 1
        return SlowCam()   # 重开后依旧低速：耗尽重开次数后应判定 slow

    monkeypatch.setattr(w, "_reopen_camera", fake_reopen)
    result = {}

    def run():
        result["outcome"] = w._run_capture_reads(SlowCam(), 0, 3, "auto")

    t = _th.Thread(target=run, daemon=True)
    t.start()
    t.join(5.0)
    assert not t.is_alive(), "帧率看门狗应在有限时间内给出结论"
    assert reopens["n"] == main_window._REOPEN_ATTEMPTS
    assert result["outcome"] == "slow"


def test_slow_outcome_advances_to_next_candidate(qapp, monkeypatch):
    """低帧率且重开无效：有候选则顺延，无候选时明确报"帧率持续过低"。"""
    w = MainWindow()
    w._recv_generation = 5
    w._capture_stop.clear()
    w._decode_error.disconnect()
    errors = []
    w._decode_error.connect(lambda m: errors.append(m))
    used = []

    class Cap:
        def set(self, *_a):
            return True

        def get(self, *_a):
            return 0

    monkeypatch.setattr(w, "_open_usable_camera",
                        lambda idx, backend, gen: (used.append(idx), Cap())[1])
    monkeypatch.setattr(w, "_run_capture_reads", lambda *a, **k: "slow")
    w._capture_loop_once([(0, "auto"), (1, "auto")], 5)
    QApplication.processEvents()
    assert used == [0, 1], "低帧率设备应顺延到下一个候选"
    assert errors and "帧率持续过低" in errors[0]


def test_report_capture_format_emits_actual_values(qapp, monkeypatch):
    """回读实际生效的宽高/帧率/编码并上报（set 不检查返回值，回读才暴露静默降级）。"""
    w = MainWindow()
    msgs = []
    w._capture_status.connect(lambda m: msgs.append(m))

    import cv2 as _cv2

    class Cap2:
        def get(self, prop):
            if prop == _cv2.CAP_PROP_FRAME_WIDTH:
                return 1280.0
            if prop == _cv2.CAP_PROP_FRAME_HEIGHT:
                return 720.0
            if prop == _cv2.CAP_PROP_FPS:
                return 60.0
            if prop == _cv2.CAP_PROP_FOURCC:
                return float(_cv2.VideoWriter_fourcc(*"MJPG"))
            return 0.0

    w._report_capture_format(Cap2(), 0)
    QApplication.processEvents()
    assert msgs and "1280×720" in msgs[0] and "60fps" in msgs[0]


def test_sustained_moderate_low_fps_keeps_running(qapp, monkeypatch):
    """重开无效但帧率仍高于 critical 阈值：只提示一次并继续接收，不中断传输。"""
    import threading as _th
    import time as _t

    import numpy as np

    from qrferry.app import main_window

    class SlowCam:
        def read(self):
            _t.sleep(0.01)
            return True, np.full((4, 4, 3), 120, dtype=np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(main_window, "_MIN_FPS", 1000.0)
    monkeypatch.setattr(main_window, "_CRITICAL_FPS", 0.0)     # 永不判定为极低
    monkeypatch.setattr(main_window, "_FPS_WARMUP_SECONDS", 0.0)
    monkeypatch.setattr(main_window, "_LOW_FPS_SECONDS", 0.05)
    monkeypatch.setattr(main_window, "_REOPEN_ATTEMPTS", 0)    # 跳过重开
    monkeypatch.setattr(main_window, "_BLACK_GRACE_SECONDS", 0.0)

    w = MainWindow()
    w._recv_generation = 4
    w._capture_stop.clear()
    w._decode_queue = None
    msgs = []
    w._capture_status.connect(lambda m: msgs.append(m))
    t = _th.Thread(target=lambda: w._run_capture_reads(SlowCam(), 0, 4, "auto"), daemon=True)
    t.start()
    deadline = _t.monotonic() + 3.0
    while _t.monotonic() < deadline and not msgs:
        QApplication.processEvents()
        _t.sleep(0.02)
    try:
        assert t.is_alive(), "非极低帧率不应停机"
        assert msgs and "帧率偏低" in msgs[0]
    finally:
        w._capture_stop.set()
        t.join(2.0)


def test_apply_capture_settings_prefers_1080p(qapp):
    """默认请求 1080p：每模块相机像素 ×1.5，是把 1200B/2200B 帧长拉回可识别的前提。

    回归背景：固定 1280×720 采集时，手机屏上的 2200B 码每模块只分到 ~3.7 相机像素
    （手机屏 1080 宽 → 码体 ~990px/157 模块 = 6.3 屏像素，再乘画面占比 ~40%），
    低于 zxing 的实际门槛，表现为「只有 700B 帧长能识别」。
    """
    import cv2

    class Cap:
        def __init__(self, w=1920, h=1080, fps=30.0):
            self.w, self.h, self.fps = w, h, fps
            self.sets = []

        def set(self, prop, val):
            self.sets.append((prop, val))
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                self.w = int(val)
            elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
                self.h = int(val)
            elif prop == cv2.CAP_PROP_FPS:
                self.fps = float(val)
            return True

        def get(self, prop):
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return float(self.w)
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return float(self.h)
            if prop == cv2.CAP_PROP_FPS:
                return self.fps
            return 0.0

    w = MainWindow()
    cap = Cap()
    w._apply_capture_settings(cap, "auto")
    assert (cap.w, cap.h) == (1920, 1080), "应优先协商 1080p"


def test_negotiate_falls_back_when_1080p_not_delivered(qapp):
    """设备把 1080p 协商成 720p 时，回读发现后自动降到 1280×720 档，不空留在假模式。"""
    import cv2

    class Cap:
        """忽略 1920 宽的请求，永远只给 1280×720（模拟低带宽 UVC）。"""

        def __init__(self):
            self.sets = []

        def set(self, prop, val):
            self.sets.append((prop, val))
            return True

        def get(self, prop):
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return 1280.0
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return 720.0
            if prop == cv2.CAP_PROP_FPS:
                return 30.0
            return 0.0

    w = MainWindow()
    cap = Cap()
    chosen = w._negotiate_capture_mode(cap)
    assert chosen[0] == 1280 and chosen[1] == 720, f"应回退到 720p 档，实际 {chosen}"
    # 回退档本身也会被尝试：宽度请求里必须出现过 1280
    width_reqs = [v for p, v in cap.sets if p == cv2.CAP_PROP_FRAME_WIDTH]
    assert 1920 in width_reqs and 1280 in width_reqs


def test_negotiate_treats_unreported_size_as_accepted(qapp):
    """后端不报告宽高（get 返回 0）时按「已接受」处理，不能误判成协商失败。"""

    class Cap:
        def set(self, prop, val):
            return True

        def get(self, prop):
            return 0.0

    w = MainWindow()
    chosen = w._negotiate_capture_mode(Cap())
    assert chosen == (1920, 1080, 30)


def test_downgrade_capture_mode_steps_down_then_stops(qapp):
    """低帧率重开前先把采集档位降一档；降到最后一档后返回 False（不再无谓重开）。"""
    from qrferry.app import main_window

    w = MainWindow()
    assert w._capture_mode_idx == 0
    last = len(main_window._CAPTURE_MODES) - 1
    steps = 0
    while w._downgrade_capture_mode():
        steps += 1
        assert steps <= last, "降档次数不应超过档位总数"
    assert w._capture_mode_idx == last, "应停在最后一档"
    assert w._downgrade_capture_mode() is False, "已在最低档不应再降"


def test_negotiate_respects_downgraded_mode(qapp):
    """降档后协商只从该档往下试，不再回头请求 1080p。"""
    import cv2

    class Cap:
        def __init__(self):
            self.widths = []

        def set(self, prop, val):
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                self.widths.append(int(val))
            return True

        def get(self, prop):
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return 1280.0
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return 720.0
            if prop == cv2.CAP_PROP_FPS:
                return 30.0
            return 0.0

    w = MainWindow()
    w._downgrade_capture_mode()
    cap = Cap()
    w._negotiate_capture_mode(cap)
    assert 1920 not in cap.widths, "降档后不应再请求 1080p"


def test_check_actual_frame_size_warns_on_mismatch(qapp):
    """驱动「报大出小」（协商 1920×1080、实出 640×480）时提示一次：这是识别率骤降的隐形原因。"""
    import numpy as np

    w = MainWindow()
    msgs = []
    w._capture_status.connect(lambda m: msgs.append(m))

    w._capture_negotiated = (1920, 1080)
    w._check_actual_frame_size(object(), 0, np.zeros((480, 640, 3), np.uint8))
    QApplication.processEvents()
    assert msgs and "实际出帧 640×480" in msgs[0]

    w._capture_negotiated = (1920, 1080)
    w._check_actual_frame_size(object(), 0, np.zeros((1080, 1920, 3), np.uint8))
    QApplication.processEvents()
    assert len(msgs) == 1, "尺寸一致时不应提示"

    w._capture_negotiated = None      # 设备不回读尺寸：不做判定
    w._check_actual_frame_size(object(), 0, np.zeros((480, 640, 3), np.uint8))
    QApplication.processEvents()
    assert len(msgs) == 1


class _FakeCap:
    """最小 VideoCapture 替身：只实现 _report_capture_format 需要的 get()。"""

    def __init__(self, w: int, h: int, fps: float, fourcc: str = "MJPG"):
        self._w, self._h, self._fps, self._fourcc = w, h, fps, fourcc

    def get(self, prop):
        import cv2
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._w)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._h)
        if prop == cv2.CAP_PROP_FPS:
            return float(self._fps)
        if prop == cv2.CAP_PROP_FOURCC:
            return float(cv2.VideoWriter_fourcc(*self._fourcc))
        return 0.0


def test_cam_meta_shows_format_and_actual_size(qapp):
    """采集实况常驻在预览标题旁：格式 + 真实出帧尺寸 + 解码耗时。

    回归背景：这些信息原先只走状态栏，而 _on_decode_progress 每帧都写「进度 x%」，
    把提示冲掉——用户在真机上根本看不到自己摄像头到底跑在什么分辨率。
    """
    w = MainWindow()
    assert w._cam_meta.text() == ""

    w._report_capture_format(_FakeCap(1280, 720, 30.0), 0)
    QApplication.processEvents()
    assert "1280×720" in w._cam_meta.text() and "30fps" in w._cam_meta.text()

    import numpy as np
    w._check_actual_frame_size(object(), 0, np.zeros((720, 1280, 3), np.uint8))
    QApplication.processEvents()
    assert "出帧 1280×720" in w._cam_meta.text()
    assert "⚠" not in w._cam_meta.text(), "尺寸一致不应告警"

    w._decode_ms = 4.25
    w._refresh_cam_meta()
    assert "解码 4.2ms" in w._cam_meta.text()


def test_cam_meta_flags_resolution_mismatch(qapp):
    """协商 1080p 但实出 640×480：常驻提示标红（这是识别率骤降的隐形原因）。"""
    import numpy as np

    w = MainWindow()
    w._report_capture_format(_FakeCap(1920, 1080, 30.0), 0)
    w._check_actual_frame_size(object(), 0, np.zeros((480, 640, 3), np.uint8))
    QApplication.processEvents()
    assert "⚠实际出帧 640×480" in w._cam_meta.text()
    assert "DC2626" in w._cam_meta.styleSheet()   # 标红


def test_dump_frame_writes_png(qapp, tmp_path, monkeypatch):
    """「保存画面」把当前帧写成 PNG；无帧时给出提示而不是报错。"""
    import numpy as np

    w = MainWindow()
    monkeypatch.setattr(w, "_debug_dir", lambda: str(tmp_path))
    w._dump_frame()
    assert "还没有画面" in w.statusBar().currentMessage()

    w._last_raw_frame = np.full((48, 64, 3), 200, dtype=np.uint8)
    w._dump_frame()
    files = list(tmp_path.glob("manual-*.png"))
    assert len(files) == 1, "应写出一张 PNG"
    assert "画面已保存" in w.statusBar().currentMessage()


def test_decode_loop_auto_dumps_failure_sample(qapp, tmp_path, monkeypatch):
    """连续整帧无码时自动留存一张原图（每轮接收有限额），便于离线定位。"""
    import queue as _q
    import threading as _th
    import time as _t

    import numpy as np

    from qrferry.app import main_window as mw

    class BlindBackend:
        def decode(self, image):
            return []          # 一直扫不到码

    from qrferry.app.receive_pipeline import ReceivePipeline

    w = MainWindow()
    monkeypatch.setattr(w, "_debug_dir", lambda: str(tmp_path))
    monkeypatch.setattr(mw, "_FAIL_DUMP_STREAK", 3)
    pipe = ReceivePipeline(backend=BlindBackend(), persist=False)
    w._pipe = pipe
    w._decode_queue = _q.Queue()
    w._recv_generation = 21
    w._decode_stop.clear()
    items = []
    w._capture_status.connect(lambda m: items.append(m))
    t = _th.Thread(target=w._decode_loop, args=(w._decode_queue, 21), daemon=True)
    t.start()
    for _ in range(12):
        w._decode_queue.put(np.full((16, 16, 3), 180, dtype=np.uint8))
        _t.sleep(0.05)
    deadline = _t.monotonic() + 2.0
    while _t.monotonic() < deadline and not list(tmp_path.glob("fail-*.png")):
        _t.sleep(0.02)
    w._decode_stop.set()
    t.join(1.0)
    QApplication.processEvents()
    files = list(tmp_path.glob("fail-*.png"))
    assert len(files) >= 1, "连续无码应自动留存样本"
    assert len(files) <= mw._FAIL_DUMP_MAX, "留存数量应受限"


def test_stall_hint_appears_on_zero_progress(qapp, monkeypatch):
    """进会话后长时间 0 进展 → 给出「调帧长/靠近」的具体建议，而不是干等。"""
    import time as _t

    from qrferry.app import main_window as mw

    class _Manifest:
        chunk_size_log = 11          # 2048B 源块（2200B 帧长）

    class _Session:
        manifest = _Manifest()

    class _Pipe:
        session = _Session()
        is_complete = False
        progress = 0.0
        elapsed_seconds = 5.0
        effective_bps = 0.0
        eta_seconds = None
        fps_instant = 0.0
        valid_data_frames = 0
        bad_frames = 0
        drop_rate = 0.0
        missed_images = 120

    w = MainWindow()
    pipe = _Pipe()
    w._pipe = pipe
    monkeypatch.setattr(mw, "_STALL_SECONDS", 0.2)

    assert not w._r_hint.isHidden() or w._r_hint.text() == ""
    w._update_r_stats()                     # 第一次：记录起点
    assert w._r_hint.text() == "", "刚开始不应立刻提示"
    _t.sleep(0.3)
    w._update_r_stats()                     # 超过阈值：出提示
    QApplication.processEvents()
    assert "700B" in w._r_hint.text()
    assert "2048B" in w._r_hint.text(), "应点明当前源块大小"
    assert not w._r_hint.isHidden(), "提示应显示"

    pipe.progress = 0.25                    # 有进展 → 提示消失
    w._update_r_stats()
    QApplication.processEvents()
    assert w._r_hint.isHidden()


def test_qr_modules_lookup_by_frame_bytes(qapp):
    """DATA 帧字节数 → 模块数查表（700B/1200B/2200B 三档）。"""
    from qrferry.app.main_window import _qr_modules_for_frame_bytes as f

    assert f(656) == 89      # 700B 帧长
    assert f(1168) == 113    # 1200B
    assert f(2192) == 157    # 2200B
    assert f(660) == 89      # 容差内
    assert f(5000) is None   # 超出所有档位


def test_module_pixels_measured_from_backend_geometry(qapp):
    """从 backend 记录的真实码宽换算「每模块像素」，这是判断该靠近还是降帧长的依据。"""
    class _Backend:
        last_code_geometry = (436.0, 1280, 656)   # 实测：700B 码宽 436px

    class _Pipe:
        backend = _Backend()

    w = MainWindow()
    w._pipe = _Pipe()
    px, modules = w._module_pixels()
    assert modules == 89
    assert abs(px - 436 / 89) < 0.01
    assert px < 5.0, "4.9px 应被判为偏小（实测正是这个值）"

    class _NoGeo:
        last_code_geometry = None

    class _Pipe2:
        backend = _NoGeo()

    w._pipe = _Pipe2()
    assert w._module_pixels() is None


def test_stall_hint_suggests_moving_closer_when_small(qapp, monkeypatch):
    """码在画面里偏小时，提示应让用户「靠近」而不是「降帧长」——两者代价完全不同。"""
    import time as _t

    from qrferry.app import main_window as mw

    class _Manifest:
        chunk_size_log = 9          # 512B 源块（700B 帧长）

    class _Session:
        manifest = _Manifest()

    class _Backend:
        last_code_geometry = (200.0, 1280, 656)   # 2.2 px/模块：明显偏小

    class _Pipe:
        session = _Session()
        backend = _Backend()
        is_complete = False
        progress = 0.0
        elapsed_seconds = 4.0
        effective_bps = 0.0
        eta_seconds = None
        fps_instant = 0.0
        valid_data_frames = 0
        bad_frames = 0
        drop_rate = 0.0
        missed_images = 50

    w = MainWindow()
    pipe = _Pipe()
    w._pipe = pipe
    monkeypatch.setattr(mw, "_STALL_SECONDS", 0.1)
    w._update_r_stats()
    _t.sleep(0.2)
    w._update_r_stats()
    QApplication.processEvents()
    hint = w._r_hint.text()
    assert "靠近" in hint, f"偏小应建议靠近，实际：{hint}"
    assert "每模块约 2.2 像素" in hint


def test_stall_hint_suggests_lower_frame_length_when_large_enough(qapp, monkeypatch):
    """码够大仍解不出 → 才建议降帧长（此时靠近无用）。"""
    import time as _t

    from qrferry.app import main_window as mw

    class _Manifest:
        chunk_size_log = 11         # 2048B 源块（2200B 帧长）

    class _Session:
        manifest = _Manifest()

    class _Backend:
        last_code_geometry = (1100.0, 1280, 2192)   # 1100/157 = 7.0 px/模块：够大

    class _Pipe:
        session = _Session()
        backend = _Backend()
        is_complete = False
        progress = 0.0
        elapsed_seconds = 4.0
        effective_bps = 0.0
        eta_seconds = None
        fps_instant = 0.0
        valid_data_frames = 0
        bad_frames = 0
        drop_rate = 0.0
        missed_images = 50

    w = MainWindow()
    w._pipe = _Pipe()
    monkeypatch.setattr(mw, "_STALL_SECONDS", 0.1)
    w._update_r_stats()
    _t.sleep(0.2)
    w._update_r_stats()
    QApplication.processEvents()
    hint = w._r_hint.text()
    assert "帧长" in hint and "700B" in hint, f"应建议降帧长，实际：{hint}"


def test_enhanced_fallback_disables_itself_when_useless():
    """三级回退试够样本仍零命中就停用——失败帧上每帧白烧 ~15ms，会拖慢整条解码。"""
    from qrferry.qr.backend import StandardQrBackend

    b = StandardQrBackend()
    assert b._enhanced_worth_trying() is True
    b._enhanced_attempts = b._ENHANCED_PROBE_FRAMES
    b._enhanced_hits = 0
    assert b._enhanced_worth_trying() is False, "零命中应停用"

    b2 = StandardQrBackend()
    b2._enhanced_attempts = b2._ENHANCED_PROBE_FRAMES
    b2._enhanced_hits = 3
    assert b2._enhanced_worth_trying() is True, "有命中应继续用"


def test_expo_autotune_keeps_short_exposure_when_better(qapp, monkeypatch):
    """短曝光让识别率明显提升时保留，并给出提示。"""
    import time as _t

    from qrferry.app import main_window as mw

    class _Pipe:
        missed_images = 0

    w = MainWindow()
    w._pipe = _Pipe()
    w._expo_state = "baseline"
    monkeypatch.setattr(mw, "_EXPO_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(mw, "_EXPO_MIN_FRAMES", 3)
    monkeypatch.setattr(w, "_try_short_exposure", lambda cap: True)
    w._reset_expo_window()      # 复刻采集循环每轮的窗口重置
    msgs = []
    w._capture_status.connect(lambda m: msgs.append(m))
    cap = object()

    # 基线窗口：约 40% 丢帧，跑到状态机离开 baseline 为止
    for i in range(12):
        if i not in (0, 3, 6, 9):
            w._pipe.missed_images += 1
        _t.sleep(0.03)
        w._expo_step(cap, None)
        if w._expo_state != "baseline":
            break
    assert w._expo_state == "trying", f"应进入试短曝光阶段，实际 {w._expo_state}"

    # 试短曝光窗口：不再丢帧 → 应保留
    for _ in range(12):
        _t.sleep(0.03)
        w._expo_step(cap, None)
        if w._expo_state == "off":
            break
    QApplication.processEvents()
    assert w._expo_state == "off"
    assert any("已启用短曝光" in m for m in msgs), msgs


def test_expo_autotune_reverts_when_no_gain(qapp, monkeypatch):
    """短曝光没有改善（甚至更差）时返回 revert，让采集循环重开设备恢复自动曝光。"""
    import time as _t

    from qrferry.app import main_window as mw

    class _Pipe:
        missed_images = 0

    w = MainWindow()
    w._pipe = _Pipe()
    w._expo_state = "baseline"
    monkeypatch.setattr(mw, "_EXPO_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(mw, "_EXPO_MIN_FRAMES", 3)
    monkeypatch.setattr(w, "_try_short_exposure", lambda cap: True)
    w._reset_expo_window()
    cap = object()

    for i in range(12):                             # 基线：约 40% 丢帧
        if i not in (0, 3, 6, 9):
            w._pipe.missed_images += 1
        _t.sleep(0.03)
        w._expo_step(cap, None)
        if w._expo_state != "baseline":
            break
    assert w._expo_state == "trying", f"应进入试短曝光阶段，实际 {w._expo_state}"

    outcome = None
    for _ in range(12):                             # 试后仍全丢：无改善
        w._pipe.missed_images += 1
        _t.sleep(0.03)
        r = w._expo_step(cap, None)
        if r:
            outcome = r
            break
    assert outcome == "revert", "无改善应回退"


def test_expo_autotune_skips_when_driver_unsupported(qapp, monkeypatch):
    """驱动不支持手动曝光时安静跳过，不影响接收。"""
    import time as _t

    from qrferry.app import main_window as mw

    class _Pipe:
        missed_images = 0

    w = MainWindow()
    w._pipe = _Pipe()
    w._expo_state = "baseline"
    monkeypatch.setattr(mw, "_EXPO_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(mw, "_EXPO_MIN_FRAMES", 3)
    monkeypatch.setattr(w, "_try_short_exposure", lambda cap: False)
    w._reset_expo_window()

    for has_code in (True, False, True, False):
        if not has_code:
            w._pipe.missed_images += 1
        _t.sleep(0.03)
        w._expo_step(object(), None)
    assert w._expo_state == "off", "不支持应直接停用，不打扰用户"

def test_negotiate_rejects_fake_60fps(qapp):
    """驱动声称接受 60fps 但回读只有 30 时，不应采用该档（60 的全部价值就是短曝光）。"""
    import cv2

    class Cap:
        """尺寸都给 1280×720；帧率永远报 30（模拟只支持 30fps 的摄像头）。"""

        def set(self, prop, val):
            return True

        def get(self, prop):
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return 1280.0
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return 720.0
            if prop == cv2.CAP_PROP_FPS:
                return 30.0
            return 0.0

    from qrferry.app import main_window as mw

    w = MainWindow()
    # 从 720p60 档开始协商
    w._capture_mode_idx = next(i for i, m in enumerate(mw._CAPTURE_MODES) if m[2] == 60)
    chosen = w._negotiate_capture_mode(Cap())
    assert chosen[2] == 30, f"假 60fps 应被拒绝，实际采用 {chosen}"


def test_negotiate_accepts_real_60fps(qapp):
    """真支持 60fps 的设备应停在 60fps 档。"""
    import cv2

    class Cap:
        def set(self, prop, val):
            return True

        def get(self, prop):
            if prop == cv2.CAP_PROP_FRAME_WIDTH:
                return 1280.0
            if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                return 720.0
            if prop == cv2.CAP_PROP_FPS:
                return 60.0
            return 0.0

    from qrferry.app import main_window as mw

    w = MainWindow()
    w._capture_mode_idx = next(i for i, m in enumerate(mw._CAPTURE_MODES) if m[2] == 60)
    chosen = w._negotiate_capture_mode(Cap())
    assert chosen[2] == 60, f"真 60fps 应采用，实际 {chosen}"


def test_color_codec_hidden_from_ui(qapp):
    """彩色码（实验）未通过验证，不得出现在任何用户可见的码制选择里。

    回归背景：该功能在真机上未验证通过，却出现在发送/接收两处的码制下拉中，
    用户选中后按实验路径发送，排障时无法区分是链路问题还是该功能本身未完成。
    """
    from qrferry.app.main_window import MainWindow as MW

    assert [code for _, code in MW.CODEC_OPTIONS] == ["qr"], "UI 只应暴露标准 QR"

    w = MainWindow()
    assert w._s_codec.count() == 1, "发送页码制下拉不应有第二项"
    assert w._s_codec.currentText() == "标准QR（推荐）"
    assert not w._s_codec.isEnabled(), "只剩一项时应禁用，避免让人以为界面坏了"
    # 接收页：整块标签 + 下拉一起隐藏（工具栏空间紧张，禁用态更突兀）
    assert w._r_codec.isHidden() and w._r_codec_label.isHidden()

    # 其它参数不受影响
    assert w._s_frame_bytes.count() == 3
    assert w._s_grid.count() == 3


def test_apply_capture_settings_falls_back_to_30fps(qapp):
    """帧率被协商成个位数时再补一次 30；已拿到 30 则不多余设置。

    目标帧率本身就是 30（拍屏优先要曝光充足、噪声低），所以协商阶梯里必然有一次
    set(FPS,30)；这里区分的是「额外兜底」——设备回读低于 30 才再设一遍。
    """
    import cv2

    class Cap:
        def __init__(self, reported):
            self.reported = reported
            self.sets = []

        def set(self, prop, val):
            self.sets.append((prop, val))
            return True

        def get(self, prop):
            return self.reported if prop == cv2.CAP_PROP_FPS else 0

    def fps30_sets(cap):
        return sum(1 for p, v in cap.sets if p == cv2.CAP_PROP_FPS and v == 30)

    w = MainWindow()
    low = Cap(7.0)
    w._apply_capture_settings(low, "auto")
    assert fps30_sets(low) == 2, "回读 7fps 应在阶梯之外再兜底一次 30"

    ok = Cap(30.0)
    w._apply_capture_settings(ok, "auto")
    assert fps30_sets(ok) == 1, "已拿到 30 不应重复设置"


def test_apply_capture_settings_skips_buffersize_on_dshow(qapp):
    """DSHOW 不支持 BUFFERSIZE（实测返回 False），跳过省一次约 400ms 的协商。"""
    import cv2

    class Cap:
        def __init__(self):
            self.props = []

        def set(self, prop, val):
            self.props.append(prop)
            return True

        def get(self, prop):
            return 60.0 if prop == cv2.CAP_PROP_FPS else 0

    w = MainWindow()
    dshow = Cap()
    w._apply_capture_settings(dshow, "dshow")
    assert cv2.CAP_PROP_BUFFERSIZE not in dshow.props
    msmf = Cap()
    w._apply_capture_settings(msmf, "msmf")
    assert cv2.CAP_PROP_BUFFERSIZE in msmf.props


def test_drop_hint_visible_only_in_file_mode(qapp):
    """拖拽提示文案常驻文件模式，切到文本模式随文件行一起隐藏。"""
    w = MainWindow()
    hint = w._s_drop_hint
    assert "拖动文件" in hint.text()
    assert "一个文件" in hint.text()
    assert not hint.isHidden(), "文件模式应显示拖拽提示"
    w._s_text.setChecked(True)
    assert hint.isHidden(), "文本模式应隐藏拖拽提示"
    w._s_file.setChecked(True)
    assert not hint.isHidden()



