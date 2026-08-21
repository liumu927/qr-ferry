"""UI 截图（offscreen）：生成 PC 端浅色/深色 × 发送/接收页 四张截图，供视觉回归。

用法: .venv\\Scripts\\python.exe scripts\\ui_screenshot.py
输出: output/ui/send_light.png / send_dark.png / recv_light.png / recv_dark.png
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from PySide6.QtWidgets import QApplication

from qrferry.app import main_window as mw


def main() -> None:
    app = QApplication.instance() or QApplication([])
    # 打桩摄像头探测：截图脚本不触碰真实设备（新代码先走 fresh 子进程探测）
    mw.probe_available_cameras_fresh = lambda max_index=9: []
    mw.probe_available_cameras = lambda max_index=9: []
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output", "ui")
    os.makedirs(out_dir, exist_ok=True)
    for dark in (False, True):
        w = mw.MainWindow()
        w._set_theme(dark)
        for tab_idx, tab_name in ((0, "send"), (1, "recv")):
            w.tabs.setCurrentIndex(tab_idx)
            w.resize(1080, 720)   # 与正式窗口默认尺寸一致
            w.show()
            # 等待 tab 淡入动画结束（180ms）再抓图，避免截到半透明瞬间
            for _ in range(6):
                app.processEvents()
                time.sleep(0.05)
            path = os.path.join(out_dir, f"{tab_name}_{'dark' if dark else 'light'}.png")
            w.grab().save(path)
            print("saved", path)
        w._quit_requested = True   # 关闭走正常清理路径（不走托盘隐藏）
        w.close()
    print("done")


if __name__ == "__main__":
    main()
