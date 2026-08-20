"""qr-ferry GUI 入口 —— 启动 PySide6 主窗口。

运行: python scripts/run_app.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# 摄像头探测子进程：主进程「刷新」时以 --probe-cameras 拉起本入口，在全新
# 进程里枚举设备（绕开 OpenCV 进程内陈旧枚举，热插拔摄像头可见），结果写
# 文件后立即退出——不进 Qt 事件循环、不碰单实例锁。
from qrferry.app import camera_devices

if camera_devices._is_probe_cameras_request():
    raise SystemExit(camera_devices._probe_cameras_main(sys.argv[1:]))

from PySide6.QtWidgets import QApplication

from qrferry.app.main_window import MainWindow


def main() -> int:
    # 单实例：已有一个 qr-ferry 进程存活时，通知它显示窗口后本进程直接退出
    # （关闭按钮最小化到托盘进程仍在，不加锁会让双击 exe 再开一个进程）。
    from qrferry.app import single_instance
    if not single_instance.is_first_instance():
        single_instance.notify_existing_instance()
        return 0
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    rc = app.exec()
    # 兜底硬退出：事件循环结束后跳过解释器 finalization（Qt/OpenCV 原生线程
    # 可能拖住进程不退出），确保托盘「退出」后进程立即终止、无后台残留。
    os._exit(rc)


if __name__ == "__main__":
    raise SystemExit(main())
