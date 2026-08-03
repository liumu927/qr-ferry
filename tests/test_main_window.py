"""主窗口 smoke 测试（offscreen，验证可构建、控件齐全、模式切换不崩溃）。"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from qrferry.app.main_window import _RECEIVE_POLL_INTERVAL_MS, MainWindow, _build_style
from qrferry.core.frame import ContentType


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _stub_camera_probe(monkeypatch):
    """主窗口构建即触发后台摄像头探测；测试环境打桩为空，避免真实打开设备。"""
    from qrferry.app import main_window
    monkeypatch.setattr(main_window, "probe_available_cameras", lambda max_index=9: [])


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


def test_receive_uses_internal_poll_rate(qapp, monkeypatch):
    """接收端使用内部采集上限，不依赖用户输入。"""
    from qrferry.app import main_window

    class FakeCamera:
        def isOpened(self):
            return True

        def set(self, *_args):
            return True

        def release(self):
            pass

    w = MainWindow()
    monkeypatch.setattr(main_window, "open_camera", lambda _index: FakeCamera())
    monkeypatch.setattr(w, "_probe_resume", lambda: None)

    w._start_recv()
    assert w._recv_timer.interval() == _RECEIVE_POLL_INTERVAL_MS
    w._stop_recv()


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
        "valid_frames": 50, "bad_frames": 5, "drop_rate": 5 / 55,
        "missed_images": 12,
        "elapsed_seconds": 10.0,
    })()
    w._recv_start_ts = 0.0
    w._update_r_stats()
    text = w._r_stats.text()
    assert "有效 50" in text
    assert "丢弃 5" in text
    assert "9.1%" in text            # 5/55 ≈ 9.09% → :.1% = "9.1%"
    assert "未识别 12" in text
    assert not w._r_stats.isHidden()


def test_receive_stats_shows_throughput_when_complete(qapp):
    """完成时统计行追加吞吐量（KB/s）。"""
    w = MainWindow()
    manifest = type("M", (), {"raw_size": 2048})()
    session = type("S", (), {"manifest": manifest})()
    result = type("R", (), {"data": b"x" * 2048, "content_type": ContentType.FILE})()
    w._pipe = type("P", (), {
        "session": session, "result": result, "progress": 1.0,
        "missing_indices": [], "is_complete": True,
        "valid_frames": 100, "bad_frames": 0, "drop_rate": 0.0,
        "missed_images": 0,
        "elapsed_seconds": 2.0,
    })()
    w._recv_start_ts = 0.0
    w._update_r_stats()
    text = w._r_stats.text()
    assert "有效 100" in text
    assert "KB/s" in text            # 完成时显示吞吐量


def test_received_file_waits_for_user_save(qapp):
    """文件完成后仅展示保存动作，不提前写入或声称已保存。"""
    w = MainWindow()
    result = type("R", (), {
        "content_type": ContentType.FILE,
        "filename": "报告.pdf",
        "data": b"pdf-data",
    })()
    session = type("S", (), {"manifest": None})()
    w._pipe = type("P", (), {"result": result, "session": session})()

    w._on_received()

    assert not w._r_save.isHidden()
    assert w._r_copy.isHidden()
    assert "文件接收完成：报告.pdf" in w._r_result.toPlainText()
    assert "已保存" not in w._r_result.toPlainText()


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
