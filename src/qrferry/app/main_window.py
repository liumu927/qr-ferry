"""PySide6 主窗口 —— 发送/接收双模式（现代风 UI，浅色/深色主题）。

发送：文件或文本 → SendController → 按帧率渲染 QR（单码或网格）到屏幕。
接收：OpenCV 摄像头采集 → ReceivePipeline → 预览 + 进度 + 完成后文本/文件。
"""
from __future__ import annotations

import os
import queue
import random
import sys
import threading
import time
from collections import deque

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import (
    QPointF,
    QPropertyAnimation,
    QRect,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qrferry import __version__
from qrferry.app.camera_devices import (
    open_camera,
    probe_available_cameras,
    probe_available_cameras_fresh,
)
from qrferry.app.compression import compression_name
from qrferry.app.file_optimizer import optimize_file
from qrferry.app.receive_pipeline import (
    ReceivePipeline,
    SessionCorruptError,
    safe_filename,
)
from qrferry.app.send_controller import (
    COLOR_MATRIX_CHUNK_SIZE_LOG,
    COLOR_MATRIX_MAX_FRAME_BYTES,
    SendController,
    SenderConfig,
    chunk_size_log_for_frame_bytes,
)
from qrferry.core.frame import ContentType
from qrferry.core.session import ReceiveSession
from qrferry.qr.backend import StandardQrBackend
from qrferry.qr.color_matrix import ColorMatrixBackend

# 采集模式阶梯：分辨率优先。逐档 set 后回读校验，第一档协商不成就降到下一档。
#
# 拍屏识别率几乎只取决于「每个 QR 模块落进几个相机像素」，而模块数随帧长跳变：
#   帧长  700B → 源块  512B → QR v18 →  89 模块
#   帧长 1200B → 源块 1024B → QR v24 → 113 模块
#   帧长 2200B → 源块 2048B → QR v35 → 157 模块
# 手机屏 1080px 宽时，码体最多给到 6~12 屏像素/模块；摄像头再按画面占比（多数人
# 对准时只占 30~50%）采样一次，1280 宽采集只剩 2.4~6.5 相机像素/模块——1200B/2200B
# 就此掉到 zxing 的实际识别门槛以下。这正是「只有 700B 帧长才识别得了」的机理，
# 不是手机屏幕小、也不是摄像头差：是接收端把可用像素丢掉了。
# 1080p 采集直接给到 1.5 倍像素；解码实测 4.5ms/帧（223fps 余量，30fps 预算充裕）。
_CAPTURE_MODES: tuple[tuple[int, int, int], ...] = (
    (1920, 1080, 30),   # 首选：分辨率优先（每模块像素 ×1.5）
    (1280, 720, 60),    # 720p 设备的最优解：60fps 强制曝光 ≤16.7ms，撕裂帧减半，
                        # 且每张码的采集机会翻倍（详见下方 _FRAME_PERIOD 说明）
    (1280, 720, 30),    # 回退：设备不支持 1080p / 60fps
)

# 解码图最大宽度：只用来挡住「远超需求的高分辨率设备」（如高拍仪强出 4K）。
# 1080p 必须原样送解码——每降采样一次就少一份模块像素，直接换识别率。
_DECODE_MAX_WIDTH = 1920

# 采集鲁棒性参数（_capture_loop / _open_usable_camera / _run_capture_reads）：
# - _OPEN_ATTEMPTS × _OPEN_PROBE_FRAMES：打开后实测「出非黑帧」的探测强度；
#   探测帧之间留短间隔，容忍 MSMF/驱动打开后传感器热启动（头几帧黑属正常）。
# - _BLACK_GRACE_SECONDS / _BLACK_CONTINUOUS_SECONDS：打开后宽限期内「连续纯黑」
#   超过阈值即判定设备无画面（虚拟摄像头空载/流切换后黑屏），自动顺延下一候选；
#   宽限期过后不再监控黑帧，避免传输中暗场景（手机息屏等）被误判为设备故障。
# - _READ_FAIL_LIMIT：连续 read 失败计数阈值（甄别 isOpened 成功但永不出帧的设备）。
_OPEN_ATTEMPTS = 3
_OPEN_PROBE_FRAMES = 8
_OPEN_PROBE_INTERVAL = 0.08
_BLACK_GRACE_SECONDS = 5.0
_BLACK_CONTINUOUS_SECONDS = 3.0
_READ_FAIL_LIMIT = 30

# 线程容错参数：
# - _DECODE_ERROR_LIMIT：连续「单帧解码异常」阈值。单帧异常（坏的帧格式/落盘 IO 抖动）
#   只跳过不杀线程；连续失败到这个量级才判定为环境性问题并停机报错。
# - _THREAD_RESTART_COOLDOWN：看门狗重启冷却。线程在这么短时间内再次死亡说明异常是
#   确定性的，继续重启只会刷日志，升级为停机报错。
# - _MAX_DECODE_RESTARTS：一次接收里允许自动重启解码线程的总次数。
_DECODE_ERROR_LIMIT = 30
_THREAD_RESTART_COOLDOWN = 2.0
_MAX_DECODE_RESTARTS = 5

# 帧率看门狗参数（_run_capture_reads / _open_usable_camera）：
# 驱动偶发落入「1 帧/秒」状态：read() 返回 ok=True 但每次精确阻塞约 1000ms，
# 且不会自愈（实测冷开 4/15；按 open→set×5→read 顺序 8/10 复现）。这种状态下
# 解码仍以 1fps 推进，肉眼表现为「进度几乎不动、像卡死」，此前没有任何检测。
# - _MIN_FPS / _LOW_FPS_SECONDS：近 3 秒实测帧率低于阈值并持续该时长 → 原地重开设备。
# - _FPS_WARMUP_SECONDS：打开后前几秒不计（自动曝光/协商还在稳定，帧率天然偏低）。
# - _REOPEN_ATTEMPTS：同一设备原地重开次数，用完仍低速则顺延下一候选。
# - _SLOW_READ_SECONDS / _SLOW_READ_LIMIT：探测期连续慢读判据。1fps 状态下每次 read
#   阻塞约 1s，8 帧探测要 8s+，必须在这一步就止损，否则"打开就很慢"。
_MIN_FPS = 5.0
_CRITICAL_FPS = 2.0      # 重开也救不回时，低于该值才停机；2~5fps 仅提示后继续（慢总比中断好）
_LOW_FPS_SECONDS = 4.0
_FPS_WARMUP_SECONDS = 2.0
_REOPEN_ATTEMPTS = 2
_SLOW_READ_SECONDS = 0.5
_SLOW_READ_LIMIT = 3
_FPS_SAMPLE_SIZE = 64    # 帧率滑动窗口样本数（1fps 时也够 60s，正常帧率下只有数秒）
_FPS_MIN_SAMPLES = 4     # 样本少于该数不判帧率（避免刚打开时误报）

# 识别失败样本留存：连续这么多帧「整帧无码」时自动存一张原图（每轮接收最多几张）。
# 真机上「扫不进去」的画面无法靠描述复现，留下原图才能离线判断是模块像素不足、
# 摩尔纹、失焦还是曝光问题。
_FAIL_DUMP_STREAK = 45
_FAIL_DUMP_MAX = 2

# 卡住判定：进会话后进度持续这么多秒为 0 就提示用户调参数（真机最常见的困惑点）
_STALL_SECONDS = 12

# 曝光 A/B 自寻优：每个窗口观察这么多秒、至少这么多帧才判定；提升不足 _EXPO_GAIN
# 就回退（拍屏丢帧主因是曝光窗口横跨换码瞬间的撕裂帧，缩短曝光可显著降低该概率）。
_EXPO_WINDOW_SECONDS = 4.0
_EXPO_MIN_FRAMES = 30
_EXPO_GAIN = 0.08

# 每个 QR 模块至少要有这么多相机像素，zxing 在拍屏场景（摩尔纹+轻微失焦）下才稳。
# 低于此值时优先建议「靠近取景」（免费增加像素），而不是先降帧长（牺牲吞吐）。
_GOOD_MODULE_PX = 5.0

# DATA 帧字节数 → QR 模块数（ECC L、byte 模式实测值）。
# 帧长固定时每帧字节数固定（144 固定开销 + 源块），因此模块数可直接查表。
_FRAME_BYTES_TO_MODULES = (
    (656, 89),    # 700B 帧长（源块 512B）
    (1168, 113),  # 1200B 帧长（源块 1024B）
    (2192, 157),  # 2200B 帧长（源块 2048B）
)


def _qr_modules_for_frame_bytes(nbytes: int) -> int | None:
    """按 DATA 帧字节数估算 QR 模块数（取最接近的档位，容差 ±48B）。"""
    best = None
    for size, modules in _FRAME_BYTES_TO_MODULES:
        if abs(nbytes - size) <= 48:
            return modules
        if best is None or abs(nbytes - size) < abs(nbytes - best[0]):
            best = (size, modules)
    return best[1] if best and abs(nbytes - best[0]) <= 200 else None
_CAPTURE_FPS_FALLBACK = 30    # 设备把帧率协商成个位数时的兜底值


def _format_eta(seconds: float) -> str:
    """ETA 秒数 → 紧凑时长串（≤60s 显示秒，否则 m/s，再大 h/m）。"""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m{sec:02d}s" if sec else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def _qimage_from_pil(img: Image.Image) -> QImage:
    if img.mode == "L":
        w, h = img.size
        return QImage(img.tobytes(), w, h, w, QImage.Format_Grayscale8).copy()
    rgb = img.convert("RGB")
    w, h = rgb.size
    return QImage(rgb.tobytes(), w, h, w * 3, QImage.Format_RGB888).copy()


def _qimage_from_cv(frame: np.ndarray) -> QImage:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    return QImage(rgb.tobytes(), w, h, 3 * w, QImage.Format_RGB888).copy()


def _make_icon() -> QIcon:
    """程序生成 QR 风格窗口图标（三个定位角 + 中心蓝块）。"""
    pix = QPixmap(64, 64)
    pix.fill(QColor("#FFFFFF"))
    p = QPainter(pix)
    dark, accent = QColor("#0F172A"), QColor("#2563EB")
    for x, y in [(6, 6), (40, 6), (6, 40)]:
        p.fillRect(QRect(x, y, 18, 18), dark)
        p.fillRect(QRect(x + 5, y + 5, 8, 8), QColor("#FFFFFF"))
    p.fillRect(QRect(32, 32, 14, 14), accent)
    p.end()
    return QIcon(pix)


def _arrow_png(path: str, up: bool, color: str) -> None:
    """生成纯色三角箭头 PNG，供 QSpinBox 上下按钮使用。"""
    pix = QPixmap(12, 8)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    if up:
        poly = QPolygonF([QPointF(6, 0), QPointF(12, 8), QPointF(0, 8)])
    else:
        poly = QPolygonF([QPointF(0, 0), QPointF(12, 0), QPointF(6, 8)])
    p.drawPolygon(poly)
    p.end()
    pix.save(path, "PNG")


# ────────────── 内嵌 SVG 图标（lucide 风 outline，{color} 占位由主题着色）──────────────
_ICON_SVGS = {
    "send": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4Z"/></svg>',
    "recv": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>'
            '<path d="M12 15V3"/></svg>',
    "stop": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<rect width="18" height="18" x="3" y="3" rx="2"/></svg>',
    "file": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
            '<path d="M14 2v6h6"/></svg>',
    "text": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h10"/></svg>',
    "folder": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
              'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
    "clear": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
             'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
             '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'
             '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
    "copy": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>'
            '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>',
    "save": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>'
            '<path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/></svg>',
    "refresh": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
               'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
               '<path d="M3 12a9 9 0 0 1 15.36-6.36L21 8"/>'
               '<path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.36 6.36L3 16"/>'
               '<path d="M3 21v-5h5"/></svg>',
    "resend": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
              'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/>'
              '<path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/></svg>',
    "resume": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
              'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>'
              '<path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>',
    "sun": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
           '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/>'
           '<path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/>'
           '<path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/>'
           '<path d="m19.07 4.93-1.41 1.41"/></svg>',
    "moon": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>',
    "help": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>'
            '<path d="M12 17h.01"/></svg>',
    "info": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
            '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
    "camera": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
              'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/>'
              '<circle cx="12" cy="13" r="3"/></svg>',
    "winmin": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
              'stroke="{color}" stroke-width="2" stroke-linecap="round">'
              '<path d="M5 12h14"/></svg>',
    "winmax": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
              'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
              '<rect x="5.5" y="5.5" width="13" height="13" rx="1.5"/></svg>',
    "winrestore": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
                  'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
                  '<rect x="7.5" y="7.5" width="11" height="11" rx="1.5"/>'
                  '<path d="M5.5 13V5.5h7.5"/></svg>',
    "winclose": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
                'stroke="{color}" stroke-width="2" stroke-linecap="round">'
                '<path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>',
}


def _svg_icon(svg: str, color: str, size: int = 16) -> QIcon:
    """SVG 字符串 → 指定颜色的 QIcon。主题切换时用当前前景/次要色重建。

    QtSvg 缺失或渲染失败时回退空 QIcon（按钮仍显示文字）。
    """
    try:
        from PySide6.QtCore import QByteArray
        from PySide6.QtSvg import QSvgRenderer

        data = QByteArray(svg.replace("{color}", color).encode("utf-8"))
        renderer = QSvgRenderer(data)
        if not renderer.isValid():
            return QIcon()
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        renderer.render(p)
        p.end()
        return QIcon(pix)
    except Exception:  # noqa: BLE001 — 图标渲染尽力而为，缺失不阻断 UI
        return QIcon()


# 边缘缩放时各方向对应的光标形状（模块级常量，避免 RUF012）
_EDGE_CURSORS = {
    None: Qt.ArrowCursor,
    "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
    "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
    "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
    "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
}


def _compression_desc(r) -> str:
    """接收结果 → 压缩信息展示文本（算法 + 原始/传输字节 + 省幅）。"""
    name = compression_name(r.compression)
    if r.raw_size > 0 and r.encoded_size > 0 and r.encoded_size < r.raw_size:
        saved = 100 * (1 - r.encoded_size / r.raw_size)
        return f"压缩: {name} {r.raw_size}→{r.encoded_size} 字节（省 {saved:.0f}%）"
    return f"压缩: {name}（内容已压缩，无进一步收益）"


class ElideLabel(QLabel):
    """按宽度自动省略的 QLabel（溢出隐藏）。默认中间省略，保留首尾（如文件名扩展名、路径首尾）。"""

    def __init__(self, text: str = "", mode=Qt.ElideMiddle):
        super().__init__()
        self._full = text
        self._mode = mode

    def fullText(self) -> str:
        return self._full

    def setFullText(self, text: str):
        self._full = text
        self._refresh()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh()

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh()

    def _refresh(self):
        fm = self.fontMetrics()
        self.setText(fm.elidedText(self._full, self._mode, max(0, self.width() - 8)))


def _check_png(path: str, color: str) -> None:
    """生成白色对勾 PNG，供 QCheckBox 选中态使用（QSS 无法直接画对勾）。"""
    from PySide6.QtGui import QPen

    pix = QPixmap(16, 16)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 2.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(3.2, 8.6), QPointF(6.8, 12.0), QPointF(12.8, 4.6)]))
    p.end()
    pix.save(path, "PNG")


def _build_style(dark: bool, up_arrow: str = "", dn_arrow: str = "", check: str = "") -> str:
    """浅色/深色两套 QSS（现代风：Tailwind slate/blue 语义色 + 渐变 + 层级卡片）。

    按钮层级：默认 QPushButton=primary（accent 渐变实心），objectName `secondary`=描边浅底。
    卡片层级：`card`=大圆角卡片，`subcard`=卡内次级分区。focus 以 accent 描边高亮。
    """
    if dark:
        bg, card, fg, sub = "#0F172A", "#1E293B", "#E2E8F0", "#94A3B8"
        accent, hover, pressed = "#3B82F6", "#60A5FA", "#2563EB"
        accent_deep, accent_soft = "#2563EB", "#1E3A8A"
        border, track, editbg, tabbar_bg = "#334155", "#334155", "#0B1220", "#0B1220"
        subcard_bg = "#0B1220"
        seg_hover = "rgba(255,255,255,0.07)"
    else:
        bg, card, fg, sub = "#F8FAFC", "#FFFFFF", "#0F172A", "#64748B"
        accent, hover, pressed = "#2563EB", "#1D4ED8", "#1E40AF"
        accent_deep, accent_soft = "#1D4ED8", "#EFF6FF"
        border, track, editbg, tabbar_bg = "#E2E8EB", "#E2E8EB", "#FFFFFF", "#F8FAFC"
        subcard_bg = "#F8FAFC"
        seg_hover = "rgba(15,23,42,0.05)"
    check_img = f"image: url({check});" if check else ""
    return f"""
QMainWindow {{ background-color: {bg}; }}
QWidget {{ color: {fg}; background: transparent;
    font-family: "Segoe UI","Microsoft YaHei",sans-serif; font-size: 14px; }}
QTabWidget {{ background: {bg}; }}
QStatusBar {{ background: {bg}; border-top: 1px solid {border}; color: {sub};
    font-size: 12px; padding: 4px 10px; }}
QStatusBar::item {{ border: none; }}
QTabWidget::pane {{ border: none; background: {bg}; top: 0px; }}
QFrame#topbar {{ background: {tabbar_bg}; border-bottom: 1px solid {border}; }}
QFrame#tabseg {{ background: {track}; border-radius: 14px; }}
QPushButton#toptab {{ background: transparent; color: {sub}; border: none;
    border-radius: 11px; padding: 8px 22px; font-weight: 600; }}
QPushButton#toptab:hover {{ color: {fg}; }}
QPushButton#toptab:checked {{ background: {card}; color: {accent}; }}
QPushButton#themeBtn {{ background: transparent; color: {sub}; border: none;
    border-radius: 12px; padding: 6px 12px; }}
QPushButton#themeBtn:hover {{ background: {track}; color: {fg}; }}
QPushButton#winbtn {{ background: transparent; border: none; border-radius: 8px; }}
QPushButton#winbtn:hover {{ background: {track}; }}
QPushButton#winbtn[closeBtn="true"]:hover {{ background: #E81123; }}
QPushButton {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {accent}, stop:1 {accent_deep}); color: #FFFFFF; border: none;
    border-radius: 12px; padding: 10px 20px; font-weight: 600; }}
QPushButton:hover {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 {hover}, stop:1 {accent}); }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:disabled {{ background: {border}; color: {sub}; }}
QPushButton#secondary {{ background: {card}; color: {fg}; border: 1px solid {border};
    border-radius: 12px; padding: 10px 20px; font-weight: 600; }}
QPushButton#secondary:hover {{ background: {accent_soft}; color: {accent};
    border-color: {accent}; }}
QPushButton#secondary:pressed {{ background: {accent_soft}; border-color: {accent}; }}
QPushButton#secondary:disabled {{ background: transparent; color: {sub}; border-color: {border}; }}
/* 预览卡头部的紧凑次要按钮（如「存帧」）：沿用 secondary 配色但收紧内边距，
   否则 10px 上下内边距 + 固定 28px 高会把文字压扁。 */
QPushButton#mini {{ background: {card}; color: {sub}; border: 1px solid {border};
    border-radius: 8px; padding: 2px 10px; font-size: 12px; }}
QPushButton#mini:hover {{ background: {accent_soft}; color: {accent}; border-color: {accent}; }}
QPushButton#mini:pressed {{ background: {accent_soft}; }}
QLineEdit, QSpinBox, QComboBox, QTextEdit {{ background: {editbg}; border: 1px solid {border};
    border-radius: 10px; padding: 8px 12px; selection-background-color: {accent}; selection-color: #FFFFFF; }}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover, QTextEdit:hover {{ border-color: {sub}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {{ border: 1px solid {accent}; }}
QSpinBox::up-button, QSpinBox::down-button {{ background: transparent; border: none; width: 20px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {border}; border-radius: 4px; }}
QSpinBox::up-arrow {{ image: url({up_arrow}); width: 10px; height: 6px; }}
QSpinBox::down-arrow {{ image: url({dn_arrow}); width: 10px; height: 6px; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::drop-down:hover {{ background: {border}; border-radius: 6px; }}
QComboBox::down-arrow {{ image: url({dn_arrow}); width: 10px; height: 6px; }}
QComboBox#paramcombo {{ padding: 6px 8px; }}          /* 参数区下拉：紧凑内边距，文字完整展示 */
QComboBox#paramcombo::drop-down {{ width: 20px; }}
QComboBox QAbstractItemView {{ background: {card}; border: 1px solid {border};
    border-radius: 10px; padding: 4px; outline: none; selection-background-color: {accent};
    selection-color: #FFFFFF; }}
QComboBox QAbstractItemView::item {{ padding: 6px 12px; min-height: 22px; border-radius: 6px; }}
QComboBox QAbstractItemView::item:selected {{ background: {accent}; color: #FFFFFF; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {sub}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {sub}; }}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; height: 0; width: 0; }}
QProgressBar {{ background: {track}; border: none; border-radius: 7px; height: 14px;
    text-align: center; color: {sub}; font-size: 11px; font-weight: 600; }}
QProgressBar::chunk {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {accent}, stop:1 #22D3EE); border-radius: 7px; }}
QCheckBox {{ color: {fg}; spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px;
    border: 1.5px solid {sub}; background: transparent; }}
QCheckBox::indicator:hover {{ border-color: {accent}; }}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; {check_img} }}
QToolTip {{ background: {card}; color: {fg}; border: 1px solid {border};
    border-radius: 8px; padding: 6px 10px; }}
QMessageBox {{ background: {card}; color: {fg}; }}
QMessageBox QLabel {{ color: {fg}; background: transparent; }}
QMessageBox QPushButton {{ min-width: 72px; }}
QFrame#card {{ background: {card}; border: 1px solid {border}; border-radius: 20px; }}
QFrame#subcard {{ background: {subcard_bg}; border: 1px solid {border}; border-radius: 14px; }}
QLabel#section-title {{ color: {sub}; font-size: 12px; font-weight: 700; letter-spacing: 1px; }}
QLabel#badge {{ background: {accent_soft}; color: {accent}; border-radius: 10px;
    padding: 4px 12px; font-size: 12px; font-weight: 600; }}
QFrame#hsep {{ background: {border}; border: none; min-height: 1px; max-height: 1px; }}
QFrame#segmented {{ background: {track}; border-radius: 12px; }}
QPushButton#seg {{ background: transparent; color: {sub}; border: none;
    border-radius: 8px; padding: 8px 10px; font-weight: 600; }}
QPushButton#seg:hover {{ color: {fg}; background: {seg_hover}; }}
QPushButton#seg:checked {{ background: {accent_soft}; color: {accent}; }}
"""


# 断点续传快照使用应用内部目录，不与用户最终选择的文件保存路径耦合。
_SENDER_STATE_DIR = os.path.expanduser("~/.qrferry")
_RECEIVER_STATE_ROOT = os.path.expanduser("~")


def _is_black_frame(frame) -> bool:
    """纯黑/近纯色帧视为「无画面」：虚拟摄像头空载常输出全黑帧（read 仍返回成功）。

    判据：灰度均值 <15 且最大像素 <40——极暗且全帧无任何亮内容。与旧版「均值+标准差」
    相比，额外检查 max：带噪点的黑帧（均值低但标准差被噪点抬高）也会被命中，不会
    再被误判成「有画面」；而暗场景里的灯/反光/亮边（max 通常 ≥40）不会被误杀。
    普通昏暗场景均值通常 >20 且有明暗差，更不会误判。
    """
    if frame is None or frame.size == 0:
        return True
    gray = cv2.cvtColor(_as_bgr(frame), cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < 15.0 and float(gray.max()) < 40.0


def _as_bgr(frame):
    """统一成 3 通道 BGR：设备/驱动可能给灰度(2D 或 (H,W,1)) 或 BGRA 帧。

    cv2.cvtColor(BGR2GRAY) 对这两种输入直接抛 cv2.error，而调用它的采集/解码
    线程一旦没有兜底就会静默死亡。
    """
    if frame.ndim == 2:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 1:
        return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    if frame.ndim == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


def _prepare_decode_image(frame, pipe):
    """解码前处理：按码制转色 + 必要时降采样。

    颜色转换按码制分支（彩色码 RGB / 标准 QR 灰度），与解码同在工作线程。
    降采样只针对「宽于 _DECODE_MAX_WIDTH」的输入（高拍仪强出 4K 之类）：1080p 采集
    必须原样解码——模块像素就是识别率，早先降到 1280 会把 2200B 帧长直接判死。
    预览走另一条路（_frame_ready），不受此处影响。
    """
    if isinstance(pipe.backend, ColorMatrixBackend):
        image = cv2.cvtColor(_as_bgr(frame), cv2.COLOR_BGR2RGB)
    else:
        image = cv2.cvtColor(_as_bgr(frame), cv2.COLOR_BGR2GRAY)
    if image.shape[1] > _DECODE_MAX_WIDTH:
        h = round(image.shape[0] * _DECODE_MAX_WIDTH / image.shape[1])
        image = cv2.resize(image, (_DECODE_MAX_WIDTH, h), interpolation=cv2.INTER_AREA)
    return image


class MainWindow(QMainWindow):
    GRID_OPTIONS = (("1×1", (1, 1)), ("2×2", (2, 2)), ("3×3", (3, 3)))
    # 彩色码（实验）未通过真机验证，暂不从界面暴露。
    # 底层实现（qr/color_matrix.py、码制分支、帧长锁定逻辑）完整保留，
    # 验证通过后把下面第二项加回去即可恢复，无需改动其它代码。
    CODEC_OPTIONS = (
        ("标准QR（推荐）", "qr"),
        # ("彩色码（实验）", "color"),
    )
    # 后台线程 → UI 线程的 queued 信号（跨线程 emit 自动排队，不在工作线程碰 widget）
    _decode_progress = Signal()
    _decode_error = Signal(str)
    _camera_probe_done = Signal(object)
    _frame_ready = Signal(object)   # 采集线程 → UI：最新帧（np.ndarray），用于刷新预览
    _capture_status = Signal(str)   # 采集线程 → UI：打开/顺延提示（黑屏自动换设备等）
    _device_in_use = Signal(int)    # 采集线程 → UI：实际出画面的设备索引（同步下拉框）

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"qr-ferry · 光学二维码摆渡 v{__version__}")
        self.setWindowIcon(_make_icon())
        # 无边框窗口：标题栏由自定义顶栏承担（拖拽 + 最小化/最大化/关闭 + 系统托盘）
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.resize(1080, 720)
        self._quit_requested = False   # 仅托盘「退出」置位；关闭按钮 → 最小化到托盘
        self._drag_off = None          # 顶栏拖拽偏移（None=未在拖拽）
        self._resize_edge = None       # 边缘缩放方向（'n','s','e','w' 组合；None=未缩放）
        self._resize_start = None      # 缩放起点 (起始 geometry, 起始全局坐标)
        # 手动最大化：Qt 6.8/Windows 上无边框窗口调用 showMaximized() 会落到
        # WindowFullScreen 状态（isMaximized() 恒 False），导致「最大化后要连点两次
        # 才还原」——用自家标志位 + 保存的普通几何，不依赖原生窗口状态机；
        # 最大化时占满所在屏幕工作区（任务栏保持可见），还原时恢复原几何。
        self._maximized = False
        self._normal_geometry = None   # 最大化前保存的普通几何（还原时恢复）
        self._backend = StandardQrBackend()
        self._send_ctrl: SendController | None = None
        self._send_iter = None
        # 发送速率统计基线（_tick_send 懒初始化；_stop_send 重置）
        self._send_start_ts: float | None = None
        self._send_block_size = 0
        self._sent_frames = 0
        self._sent_bytes = 0
        self._send_timer = QTimer(self)
        self._send_timer.timeout.connect(self._tick_send)
        # 接收统计定时刷新：无新帧时速率/ETA 仍按时间推移更新（滑动窗口左移、ETA 倒计时）
        self._r_stats_timer = QTimer(self)
        self._r_stats_timer.timeout.connect(self._update_r_stats)
        self._r_stats_timer.timeout.connect(self._check_recv_threads)
        self._pipe: ReceivePipeline | None = None
        self._cap = None
        # 采集/解码均在工作线程，UI 不碰 cap：_capture_loop 负责 open+read+release，
        # 帧经 _frame_ready 回 UI 刷新预览、经 _decode_queue 喂 _decode_loop。避免 open/read 阻塞 UI。
        self._capture_stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        # 后台解码线程状态：queue 为"只保留最新一帧"的槽位，_decode_stop 控制退出
        self._decode_queue: queue.Queue | None = None
        self._decode_stop = threading.Event()
        self._decode_thread: threading.Thread | None = None
        # 看门狗状态：解码线程异常退出后自动重启的次数与上次重启时刻（冷却判断）
        self._decode_restarts = 0
        self._last_decode_restart = 0.0
        # 采集档位（_CAPTURE_MODES 下标）：从最优先（1080p）开始，帧率不达标时自动降档
        self._capture_mode_idx = 0
        # 协商得到的 (宽, 高)：_report_capture_format 写入，首帧到达时与其核对
        self._capture_negotiated: tuple[int, int] | None = None

        # 采集实况诊断（预览标题旁常驻显示）
        self._cam_format = ""
        self._cam_actual: tuple[int, int] | None = None
        self._decode_ms = 0.0          # 最近一帧解码耗时（工作线程写、UI 线程读）
        self._last_raw_frame = None    # 最新原始帧（「保存画面」用）
        self._fail_dumped = 0          # 本次接收已自动留存失败样本数
        self._fail_streak = 0          # 连续「整帧无码」计数（触发自动留存）
        self._stall_since: float | None = None   # 零进展起始时刻（卡住提示用）
        self._hint_session_id: int | None = None  # 卡住提示所属会话（换会话即复位）
        # 曝光 A/B 自寻优状态（见 _expo_step）
        self._expo_state = "off"
        self._expo_base_rate = 0.0
        self._expo_t0 = 0.0
        self._expo_frames = 0
        self._expo_clean = 0
        self._expo_last_missed = 0
        # 接收代际令牌：_stop_recv 的 join 超时后（MSMF read 慢时常见）旧线程可能
        # 尚未退出，新一轮 clear() 会让它"复活"，把旧队列残留帧喂进新 pipe——
        # 首帧时间戳被提前打上，"已用"起始即非零；还可能弹旧会话的错误框。
        # 每轮启停自增代数，线程闭包持有自己的代数，不匹配即静默退出且不再 emit。
        self._recv_generation = 0
        self._decode_progress.connect(self._on_decode_progress)
        self._decode_error.connect(self._on_decode_error)
        self._camera_probe_done.connect(self._on_camera_probe_done)
        self._frame_ready.connect(self._on_frame_ready)
        self._capture_status.connect(lambda msg: self.statusBar().showMessage(msg))
        self._device_in_use.connect(self._on_device_in_use)
        self._file_path = ""
        self._dark = False
        self._cam_selected_idx: int | None = None   # 用户选择的摄像头索引（跨刷新保留）
        self._r_cam_backends: dict[int, str] = {}   # 下拉框索引 → 探测成功的后端（热插拔采集复用）
        # 单实例：第二实例经注册窗口消息请求显示主窗口（本进程即首实例，监听之）
        from qrferry.app import single_instance
        self._show_win_msg = single_instance.register_show_message()
        self._build_ui()
        self._build_tray()
        self._apply_theme()
        self._apply_window_corners()   # Win11 DWM 圆角：普通态圆角、最大化态直角

    # ────────────── 系统托盘 ──────────────
    def _build_tray(self):
        """系统托盘：favicon 图标 + 「显示窗口 / 退出」菜单；关闭按钮只隐藏到托盘。"""
        self._tray = QSystemTrayIcon(_make_icon(), self)
        self._tray.setToolTip(f"qr-ferry · 光学二维码摆渡 v{__version__}")
        menu = QMenu()
        act_show = menu.addAction("显示窗口")
        act_show.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._quit_app)
        self._tray.setContextMenu(menu)
        # 单击托盘图标恢复窗口
        self._tray.activated.connect(
            lambda reason: self._show_from_tray() if reason == QSystemTrayIcon.Trigger else None)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()

    def _show_from_tray(self):
        """托盘 → 恢复窗口（普通/最大化状态保持，置顶激活）。"""
        self.showNormal() if self.isMinimized() else self.show()
        self.raise_()
        self.activateWindow()

    def _quit_app(self):
        """托盘「退出」：置退出标志后关闭窗口，走 closeEvent 正常清理线程。

        close() 同步完成清理后立即 os._exit 硬退出：Qt/OpenCV 的原生线程
        （摄像头采集、DWM 合成等）在解释器收尾阶段可能拖住进程不退出——
        表现为「托盘退出后后台程序残留」。跳过 finalization 可保证进程立即终止，
        会话状态已在传输过程中增量落盘，无数据丢失。
        """
        self._quit_requested = True
        self._tray.hide()
        self.close()
        os._exit(0)   # 兜底硬退出：杜绝后台进程残留

    def closeEvent(self, ev):
        """关闭按钮 → 最小化到托盘（不退出）；仅托盘「退出」才真正结束。"""
        if not self._quit_requested:
            ev.ignore()
            self.hide()
            if QSystemTrayIcon.isSystemTrayAvailable():
                self._tray.showMessage(
                    "qr-ferry", "已最小化到系统托盘\n右键托盘图标选择「退出」以结束程序",
                    QSystemTrayIcon.Information, 2500)
            return
        self._stop_send()
        self._stop_recv()
        super().closeEvent(ev)

    # ────────────── 窗口控制（无边框）──────────────
    def _toggle_maximize(self):
        """最大化/还原切换（手动几何实现，规避 Qt6/Windows 无边框状态错乱）。

        无边框窗口调用 showMaximized() 在 Qt 6.8/Windows 上会进入 WindowFullScreen
        状态（isMaximized() 返回 False），原实现把「已最大化」误判为「未最大化」：
        第一次还原点击实际执行的是再次 showMaximized()（外观无变化），第二次才真正
        还原——即用户感知的「需要点两次」。这里改为自记标志位 + 保存普通几何：
        最大化 setGeometry 到所在屏幕工作区（任务栏可见），还原恢复原几何，行为确定。
        """
        if self._maximized:
            if self._normal_geometry is not None:
                self.setGeometry(self._normal_geometry)
            self._maximized = False
        else:
            self._normal_geometry = self.geometry()
            scr = self.screen()
            self.setGeometry(scr.availableGeometry() if scr else self.geometry())
            self._maximized = True
        self._update_max_btn()
        self._apply_window_corners()

    def _update_max_btn(self):
        if hasattr(self, "_win_max"):
            fg = "#E2E8F0" if self._dark else "#0F172A"
            icon = "winrestore" if self._maximized else "winmax"
            self._win_max.setIcon(_svg_icon(_ICON_SVGS[icon], fg, 14))
            self._win_max.setToolTip("还原" if self._maximized else "最大化")

    def showEvent(self, e):
        super().showEvent(e)
        self._apply_window_corners()   # 每次显示（含托盘恢复）确保 DWM 圆角生效

    def _apply_window_corners(self):
        """Windows 11 DWM 圆角：普通态圆角、最大化态直角（与系统最大化行为一致）。

        DWM 按 HWND 记忆圆角偏好，Qt 在个别场景会重建原生窗口，故每次显示/切换
        最大化时重设。非 Win11 或调用失败静默跳过（保持直角，不阻断 UI）。
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            # DWMWA_WINDOW_CORNER_PREFERENCE=33；DWMWCP_ROUND=2 / DWMWCP_DONOTROUND=1
            pref = ctypes.c_int(2 if not self._maximized else 1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:  # noqa: BLE001, S110 — 圆角是装饰性增强，失败静默回退直角
            pass

    # ────────────── 使用说明 ──────────────
    def _show_help(self):
        """顶栏「?」按钮：弹窗展示普通用户向的简明使用说明。"""
        QMessageBox.information(
            self, "使用说明 · qr-ferry",
            "<b>一、发送（屏幕显示二维码，另一端扫码接收）</b><br>"
            "1. 选「文件」或「文本」，准备好内容；<br>"
            "2. 点「开始发送」，屏幕显示二维码；<br>"
            "3. 另一端（电脑或手机）的摄像头对准扫码即可，发送中可随时停止。<br><br>"
            "<b>二、接收（用摄像头扫描另一端屏幕）</b><br>"
            "1. 切到「接收」页，确认摄像头已选好，能正常看到画面；<br>"
            "2. 让另一端屏幕的二维码对准摄像头，保持 15~30cm；<br>"
            "3. 完成后文本可直接复制，文件点「保存文件」选择位置。<br><br>"
            "<b>三、小提示</b><br>"
            "· 设备不限：电脑与电脑、手机与手机、电脑与手机都可以互传；<br>"
            "· 二维码与摄像头保持 15~30cm，对焦清晰、光线均匀成功率最高；<br>"
            "· 发送较慢时调低帧率（如 8 FPS）更稳，调高帧率（如 24/30）更快；<br>"
            "· 摄像头分辨率较低、识别不稳时，把帧长调低（如 700B）更容易识别——"
            "帧长越大，二维码模块越密，每个模块能分到的相机像素越少；<br>"
            "· 传输中断可重新点「开始发送/接收」，已收到的部分会自动续传；<br>"
            "· 关闭窗口会最小化到托盘，右键托盘图标可「退出」。")

    def _show_param_help(self):
        """参数区叹号：弹窗集中说明各参数含义（普通用户向）。"""
        QMessageBox.information(
            self, "参数说明",
            "<b>帧率</b>：每秒显示多少张二维码。越高传得越快，但码切换太快容易扫不准；"
            "扫不稳时调低（如 8 FPS）更稳。<br><br>"
            "<b>码制</b>：当前版本固定使用标准 QR（兼容性最好）。"
            "彩色码为实验功能，因尚未通过验证暂不提供。<br><br>"
            "<b>纠错</b>：二维码的容错等级。越高越耐遮挡、模糊，但码点更密、更难扫；"
            "一般用 L 或 M 即可。<br><br>"
            "<b>帧长</b>：每张二维码携带的数据量。帧长越大，码点越密、每个码点能分到的"
            "相机像素越少，越容易扫不出（尤其用手机屏幕当发送端时）——扫不稳就选 700B；"
            "电脑显示器当发送端、或摄像头分辨率高时可用 1200/2200B 传得更快。<br><br>"
            "<b>冗余</b>：发送时额外多发的纠错数据比例。越高越抗丢帧、传输更稳，但更慢；"
            "链路稳定时 1.2~1.5 即可。<br><br>"
            "<b>网格</b>：一屏显示几个二维码。2×2/3×3 一屏数据更多、更快，但每个码更小，"
            "需要接收端离得近一些。")

    # ────────────── 主题 ──────────────
    def _apply_theme(self):
        import tempfile
        sub = "#94A3B8" if self._dark else "#64748B"
        d = tempfile.gettempdir()
        up = os.path.join(d, "qrferry_spin_up.png").replace("\\", "/")
        dn = os.path.join(d, "qrferry_spin_dn.png").replace("\\", "/")
        ck = os.path.join(d, "qrferry_check.png").replace("\\", "/")
        _arrow_png(up, True, sub)
        _arrow_png(dn, False, sub)
        _check_png(ck, "#FFFFFF")
        self.setStyleSheet(_build_style(self._dark, up, dn, ck))
        # 区域背景随主题
        editbg = "#0B1220" if self._dark else "#FFFFFF"
        border = "#334155" if self._dark else "#E2E8EB"
        sub_txt = "#94A3B8" if self._dark else "#64748B"
        qr_bg = "#1E293B" if self._dark else "#FFFFFF"      # QR 区：浅色白、深色卡片深（QR 图自带白底居中显示）
        cam_bg = "#1E293B" if self._dark else "#E2E8F0"     # 预览区：浅色浅灰、深色深
        self._qr_label.setStyleSheet(
            f"background:{qr_bg}; border:1px solid {border}; border-radius:20px; color:{sub_txt};")
        self._cam_label.setStyleSheet(
            f"background:{cam_bg}; border:1px solid {border}; border-radius:20px; color:{sub_txt};")
        if hasattr(self, "_s_drop_hint"):
            self._s_drop_hint.setStyleSheet(f"color:{sub_txt}; font-size:11px;")
        # QTextEdit viewport 背景随主题（QSS background 对 viewport 有时不生效）
        for te in (self._s_textedit, self._r_result):
            te.viewport().setStyleSheet(f"background:{editbg};")
        self._rebuild_icons()

    def _rebuild_icons(self):
        """主题切换时按当前前景/次要色重建全部矢量图标。"""
        fg = "#E2E8F0" if self._dark else "#0F172A"
        sub = "#94A3B8" if self._dark else "#64748B"
        for btn, name in getattr(self, "_icon_buttons", []):
            btn.setIcon(_svg_icon(_ICON_SVGS[name], sub, 16))
            btn.setIconSize(QSize(16, 16))
        if hasattr(self, "_theme_btn"):
            name = "sun" if self._dark else "moon"
            self._theme_btn.setIcon(_svg_icon(_ICON_SVGS[name], sub, 18))
            self._theme_btn.setIconSize(QSize(18, 18))
        if hasattr(self, "_help_btn"):
            self._help_btn.setIcon(_svg_icon(_ICON_SVGS["help"], sub, 18))
            self._help_btn.setIconSize(QSize(18, 18))
        if hasattr(self, "_param_help_btn"):
            self._param_help_btn.setIcon(_svg_icon(_ICON_SVGS["info"], sub, 15))
            self._param_help_btn.setIconSize(QSize(15, 15))
        # 窗口控制按钮：常态用前景色（清晰可辨），hover 高亮由 eventFilter 切换
        if hasattr(self, "_win_min"):
            self._win_min.setIcon(_svg_icon(_ICON_SVGS["winmin"], fg, 14))
            self._win_min.setIconSize(QSize(14, 14))
            self._win_max.setIcon(_svg_icon(
                _ICON_SVGS["winrestore" if self._maximized else "winmax"], fg, 14))
            self._win_max.setIconSize(QSize(14, 14))
            self._win_close.setIcon(_svg_icon(_ICON_SVGS["winclose"], fg, 14))
            self._win_close.setIconSize(QSize(14, 14))
        self._update_running_icons()
        self._update_content_seg_icons()

    def _update_content_seg_icons(self):
        """内容分段（文件/文本）图标：选中项 accent 色、未选中次要色，随主题/切换刷新。"""
        if not hasattr(self, "_s_file"):
            return
        sub = "#94A3B8" if self._dark else "#64748B"
        accent = "#3B82F6" if self._dark else "#2563EB"
        for btn, name in ((self._s_file, "file"), (self._s_text, "text")):
            color = accent if btn.isChecked() else sub
            btn.setIcon(_svg_icon(_ICON_SVGS[name], color, 16))
            btn.setIconSize(QSize(16, 16))

    def _update_running_icons(self):
        """主操作按钮（primary 渐变底）图标用白色，按运行态切换 stop/send、stop/recv。"""
        if hasattr(self, "_s_start"):
            running = self._s_start.isChecked()
            self._s_start.setIcon(_svg_icon(_ICON_SVGS["stop" if running else "send"], "#FFFFFF", 16))
            self._s_start.setIconSize(QSize(16, 16))
        if hasattr(self, "_r_start"):
            running = self._r_start.isChecked()
            self._r_start.setIcon(_svg_icon(_ICON_SVGS["stop" if running else "recv"], "#FFFFFF", 16))
            self._r_start.setIconSize(QSize(16, 16))

    def _set_theme(self, dark: bool):
        self._dark = dark
        self._apply_theme()
        if hasattr(self, "_theme_btn"):
            self._theme_btn.setToolTip("切换到浅色" if dark else "切换到深色")

    # ────────────── UI 构建 ──────────────
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶栏：品牌区（logo + 标题 + 版本）| 居中胶囊 tab | 主题切换 | 窗口控制
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(56)
        self._topbar = topbar
        tbar = QHBoxLayout(topbar)
        tbar.setContentsMargins(18, 0, 14, 0)
        tbar.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(_make_icon().pixmap(24, 24))
        tbar.addWidget(logo)
        brand = QLabel("qr-ferry")
        brand.setStyleSheet("font-size:15px; font-weight:700;")
        tbar.addWidget(brand)
        brand_sub = QLabel(f"光学二维码摆渡 · v{__version__}")
        brand_sub.setStyleSheet("color:#64748B; font-size:11px;")
        tbar.addWidget(brand_sub)
        tbar.addStretch(1)

        # 胶囊 tab：QFrame#tabseg 容器内两个 checkable 按钮
        tabseg = QFrame()
        tabseg.setObjectName("tabseg")
        tabseg.setFixedHeight(38)
        seg_lay = QHBoxLayout(tabseg)
        seg_lay.setContentsMargins(4, 4, 4, 4)
        seg_lay.setSpacing(4)
        self._tab_send = QPushButton("发送")
        self._tab_send.setObjectName("toptab")
        self._tab_send.setCheckable(True)
        self._tab_send.setChecked(True)
        self._tab_send.setToolTip("切换到发送页：文件或文本 → 屏幕 QR 码播放")
        self._tab_recv = QPushButton("接收")
        self._tab_recv.setObjectName("toptab")
        self._tab_recv.setCheckable(True)
        self._tab_recv.setToolTip("切换到接收页：摄像头扫描 QR 码恢复文件/文本")
        tgrp = QButtonGroup(tabseg)
        tgrp.addButton(self._tab_send)
        tgrp.addButton(self._tab_recv)
        self._tab_send.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        self._tab_recv.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        seg_lay.addWidget(self._tab_send)
        seg_lay.addWidget(self._tab_recv)
        tbar.addWidget(tabseg)
        tbar.addStretch(1)

        # 使用说明按钮（顶栏右侧、主题按钮左侧）：弹窗展示普通用户向的简明说明
        self._help_btn = QPushButton()
        self._help_btn.setObjectName("themeBtn")
        self._help_btn.setCursor(Qt.PointingHandCursor)
        self._help_btn.setToolTip("使用说明")
        self._help_btn.setIcon(_svg_icon(_ICON_SVGS["help"], "#64748B", 18))
        self._help_btn.setIconSize(QSize(18, 18))
        self._help_btn.clicked.connect(self._show_help)
        tbar.addWidget(self._help_btn)

        # 主题切换按钮（顶栏右侧，SVG 图标）
        self._theme_btn = QPushButton()
        self._theme_btn.setObjectName("themeBtn")
        self._theme_btn.setCursor(Qt.PointingHandCursor)
        self._theme_btn.setToolTip("切换到深色")
        self._theme_btn.clicked.connect(lambda: self._set_theme(not self._dark))
        tbar.addWidget(self._theme_btn)
        tbar.addSpacing(6)

        # 窗口控制按钮（无边框窗口的自定义标题栏：最小化 / 最大化 / 关闭）
        # 用 SVG 矢量图标（不依赖字体字符，任意平台一致渲染），颜色随主题重建
        self._win_min = QPushButton()
        self._win_min.setObjectName("winbtn")
        self._win_min.setToolTip("最小化")
        self._win_min.setIcon(_svg_icon(_ICON_SVGS["winmin"], "#64748B", 14))
        self._win_min.setIconSize(QSize(14, 14))
        self._win_min.clicked.connect(self.showMinimized)
        self._win_max = QPushButton()
        self._win_max.setObjectName("winbtn")
        self._win_max.setToolTip("最大化")
        self._win_max.setIcon(_svg_icon(_ICON_SVGS["winmax"], "#64748B", 14))
        self._win_max.setIconSize(QSize(14, 14))
        self._win_max.clicked.connect(self._toggle_maximize)
        self._win_close = QPushButton()
        self._win_close.setObjectName("winbtn")
        self._win_close.setProperty("closeBtn", True)
        self._win_close.setToolTip("关闭（最小化到托盘）")
        self._win_close.setIcon(_svg_icon(_ICON_SVGS["winclose"], "#64748B", 14))
        self._win_close.setIconSize(QSize(14, 14))
        self._win_close.clicked.connect(self.close)
        for wb in (self._win_min, self._win_max, self._win_close):
            wb.setCursor(Qt.PointingHandCursor)
            wb.setFixedSize(38, 30)
            wb.installEventFilter(self)   # hover 图标高亮（关闭按钮换白）
            tbar.addWidget(wb)
        root.addWidget(topbar)

        # 内容区：QTabWidget 隐藏自带 tab bar，由顶栏按钮驱动切换（带淡入动画）
        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.addTab(self._build_send_tab(), "发送")
        self.tabs.addTab(self._build_recv_tab(), "接收")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.setAcceptDrops(True)   # 整窗接收文件拖拽（见 dragEnterEvent/dropEvent）
        self.statusBar().showMessage("就绪")

    def _on_tab_changed(self, _index: int):
        """页面切换淡入（QGraphicsOpacityEffect + QPropertyAnimation，防 GC 持有引用）。

        起始透明度 0.35 而非 0：避免切换瞬间整页闪空（opacity=0 时连背景都看不到）。
        """
        w = self.tabs.currentWidget()
        if w is None:
            return
        eff = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(180)
        anim.setStartValue(0.35)
        anim.setEndValue(1.0)
        anim.finished.connect(lambda: w.setGraphicsEffect(None))
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._tab_anim = anim   # 防动画被 GC：DeleteWhenStopped 结束后自释放

    def _build_send_tab(self) -> QWidget:
        tab = QWidget()
        lay = QHBoxLayout(tab)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(20)
        self._icon_buttons = []

        def icon_btn(text: str, icon: str) -> QPushButton:
            """创建带矢量图标的按钮并登记（主题切换时 _rebuild_icons 重建图标色）。"""
            b = QPushButton(text)
            self._icon_buttons.append((b, icon))
            return b

        def section(title: str) -> QLabel:
            lb = QLabel(title)
            lb.setObjectName("section-title")
            return lb

        # ── 左：控制卡（内容 / 参数 / 操作 三个分区）──
        # 428 宽：参数两列各自有足够宽度展示下拉框完整文字（"标准QR（推荐）"等），
        # 右侧预览卡仍有余量（QR 区 ≥400px 最小宽）。
        left_w = QFrame()
        left_w.setObjectName("card")
        left_w.setFixedWidth(428)
        left = QVBoxLayout(left_w)
        left.setContentsMargins(16, 12, 16, 12)
        left.setSpacing(6)

        left.addWidget(section("内容"))
        # 分段控件：文件 / 文本（图标 + 文字，选中态 accent 浅底，等宽两分）
        seg = QFrame()
        seg.setObjectName("segmented")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(4, 4, 4, 4)
        sl.setSpacing(4)
        self._s_file = QPushButton("文件")
        self._s_file.setObjectName("seg")
        self._s_file.setCheckable(True)
        self._s_file.setChecked(True)
        self._s_text = QPushButton("文本")
        self._s_text.setObjectName("seg")
        self._s_text.setCheckable(True)
        bgrp = QButtonGroup(seg)
        bgrp.addButton(self._s_file)
        bgrp.addButton(self._s_text)
        self._s_file.toggled.connect(self._on_send_mode)
        self._s_file.toggled.connect(self._update_content_seg_icons)
        self._s_text.toggled.connect(self._update_content_seg_icons)
        sl.addWidget(self._s_file, 1)
        sl.addWidget(self._s_text, 1)
        left.addWidget(seg)
        self._update_content_seg_icons()

        # 文件状态（左）+ 选择按钮（右），同行；按钮与「清空」等宽
        file_row = QHBoxLayout()
        self._s_filelabel = ElideLabel("（未选择）", Qt.ElideMiddle)
        self._s_filelabel.setStyleSheet("color:#64748B;")
        self._s_filelabel.setToolTip("（未选择）")
        file_row.addWidget(self._s_filelabel, 1)
        self._s_pick = icon_btn("选择", "folder")
        self._s_pick.setObjectName("secondary")
        self._s_pick.clicked.connect(self._pick_file)
        self._s_pick.setFixedWidth(104)
        file_row.addWidget(self._s_pick)
        left.addLayout(file_row)

        # 拖拽提示（静态说明）：只在文件模式显示，颜色随主题切换（见 _apply_theme）
        self._s_drop_hint = QLabel("可直接拖动文件到此处，每次仅能传递一个文件")
        self._s_drop_hint.setStyleSheet("color:#64748B; font-size:11px;")
        self._s_drop_hint.setToolTip(
            "把单个文件拖到窗口任意位置即可载入，会自动切到发送页。\n"
            "发送过程中不支持更换文件。")
        left.addWidget(self._s_drop_hint)

        # 文件优化：无损（OOXML 重打包/PDF 重压缩）选择文件后自动执行；
        # 勾选下方选项才额外允许有损转码（照片转 JPEG 等，会改变接收到的文件内容/格式）
        self._s_optimize = QCheckBox("允许有损优化（会改变接收文件）")
        self._s_optimize.setToolTip(
            "无损优化（DOCX/XLSX/PPTX 重打包、PDF 流重压缩）自动执行，不改变文件内容。\n"
            "勾选后额外允许有损转码：照片 PNG 转 JPEG、截图调色板量化、OOXML 内嵌图片压缩——\n"
            "接收端保存的文件与原文件不同（格式/质量/透明通道），请仅在可接受时才勾选。")
        self._s_optimize.toggled.connect(self._update_optimize_preview)
        left.addWidget(self._s_optimize)

        self._s_textedit = QTextEdit()
        self._s_textedit.setPlaceholderText("输入要发送的文本…")
        self._s_textedit.setMaximumHeight(100)
        # 拖文件时不让文本框吞掉 drop（QTextEdit 默认会插入路径文本），交给窗口统一处理
        self._s_textedit.setAcceptDrops(False)
        self._s_textedit.hide()
        left.addWidget(self._s_textedit)
        # 字符统计角标（框内右下角，不计字符限制）
        self._s_count = QLabel("0 字符", self._s_textedit)
        self._s_count.setStyleSheet("color:#64748B; background:rgba(255,255,255,0.85); padding:1px 6px; border-radius:6px; font-size:11px;")
        self._s_textedit.textChanged.connect(self._update_s_count)
        self._s_textedit.viewport().installEventFilter(self)
        # 清空按钮（文本模式时随文本框显示，右对齐）
        clear_row = QHBoxLayout()
        clear_row.addStretch(1)
        self._s_clear = icon_btn("清空", "clear")
        self._s_clear.setObjectName("secondary")
        self._s_clear.clicked.connect(self._s_textedit.clear)
        self._s_clear.setFixedWidth(104)
        self._s_clear.hide()
        clear_row.addWidget(self._s_clear)
        left.addLayout(clear_row)

        hsep = QFrame(); hsep.setObjectName("hsep"); left.addWidget(hsep)

        # 「参数」标题行：右侧叹号按钮，集中查看全部参数说明（悬浮/点击均可）
        param_head = QHBoxLayout()
        param_head.addWidget(section("参数"))
        param_head.addStretch(1)
        self._param_help_btn = QPushButton()
        self._param_help_btn.setObjectName("themeBtn")
        self._param_help_btn.setCursor(Qt.PointingHandCursor)
        self._param_help_btn.setIcon(_svg_icon(_ICON_SVGS["info"], "#64748B", 15))
        self._param_help_btn.setIconSize(QSize(15, 15))
        self._param_help_btn.setToolTip(
            "查看各参数含义（点击查看完整说明）\n"
            "· 帧率：每秒显示的码数，越高越快、越低越稳\n"
            "· 码制：当前固定标准 QR（彩色码未通过验证，暂不提供）\n"
            "· 纠错：越高越耐遮挡模糊，但码更密\n"
            "· 帧长：单码数据量，码点密易扫不出，扫不稳选 700\n"
            "· 冗余：越高越抗丢帧，但更慢\n"
            "· 网格：一屏码数，越大越快但码更小")
        self._param_help_btn.clicked.connect(self._show_param_help)
        param_head.addWidget(self._param_help_btn)
        left.addLayout(param_head)

        # 参数区两列网格：卡片加宽 + 标签收窄 + 列间距加大，保证下拉框文字完整显示
        # （"标准QR（推荐）" 等长项不再被截断），且右侧参数名称与左侧控件拉开距离。
        param_specs = (
            ("帧率", "_s_fps", None, 12),
            ("码制", "_s_codec", [n for n, _ in self.CODEC_OPTIONS], None),
            ("纠错", "_s_ecc", ["L", "M", "Q", "H"], "L"),
            ("帧长", "_s_frame_bytes", ["700", "1200", "2200"], "1200"),
            ("冗余", "_s_redundancy", ["1.2", "1.5", "2.0"], "1.5"),
            ("网格", "_s_grid", [n for n, _ in self.GRID_OPTIONS], None),
        )
        for i in range(0, 6, 2):
            row = QHBoxLayout()
            row.setSpacing(16)   # 两列之间的间距（右侧标签不再贴着左侧控件）
            for label, attr, items, cur in param_specs[i:i + 2]:
                cell = QHBoxLayout()
                cell.setSpacing(8)
                lb = QLabel(label)
                lb.setFixedWidth(40)   # 标签均为两字，40px 足够
                cell.addWidget(lb)
                if attr == "_s_fps":
                    w = QSpinBox(); w.setRange(1, 30); w.setValue(cur)
                else:
                    w = QComboBox(); w.addItems(items)
                    if cur is not None: w.setCurrentText(cur)
                    w.setObjectName("paramcombo")   # 紧凑内边距（见 _build_style）
                w.setFixedHeight(34)
                setattr(self, attr, w)
                # 只有一个可选项时禁用下拉：彩色码暂不暴露后「码制」只剩标准 QR，
                # 可点开却无其它选项会让人以为界面坏了。
                if isinstance(w, QComboBox) and len(items) <= 1:
                    w.setEnabled(False)
                    w.setToolTip("彩色码（实验）已知问题未解决，当前版本仅提供标准 QR")
                cell.addWidget(w, 1)
                row.addLayout(cell, 1)
            left.addLayout(row)
        self._s_codec.currentIndexChanged.connect(self._sync_send_codec_controls)
        self._sync_send_codec_controls()

        # 人工补发：会话存活时可追加 N 个新符号（LT 喷泉码靠新 symbol_id 补缺块，非重发旧帧）
        extra_row = QHBoxLayout()
        extra_lb = QLabel("补发帧数")
        extra_lb.setFixedWidth(56)
        extra_row.addWidget(extra_lb)
        self._s_extra = QSpinBox(); self._s_extra.setRange(1, 10000); self._s_extra.setValue(50)
        self._s_extra.setFixedHeight(34)
        extra_row.addWidget(self._s_extra, 1)
        self._s_resend = icon_btn("补发", "resend")
        self._s_resend.setObjectName("secondary")
        self._s_resend.clicked.connect(self._resend)
        extra_row.addWidget(self._s_resend)
        left.addLayout(extra_row)

        hsep2 = QFrame(); hsep2.setObjectName("hsep"); left.addWidget(hsep2)

        left.addWidget(section("操作"))
        # 断点续传已并入「开始发送」：发送前自动探测未完成会话并询问（见 _probe_resume_send）
        self._s_start = icon_btn("开始发送", "send")
        self._s_start.setCheckable(True)
        self._s_start.toggled.connect(self._toggle_send)
        self._s_start.setMinimumHeight(46)
        left.addWidget(self._s_start)
        left.addStretch(1)
        lay.addWidget(left_w)

        # ── 右：发送预览卡（标题 + 状态徽标 + QR 区）──
        right_w = QFrame()
        right_w.setObjectName("card")
        right = QVBoxLayout(right_w)
        right.setContentsMargins(20, 16, 20, 18)
        right.setSpacing(14)
        head = QHBoxLayout()
        title = QLabel("发送预览")
        title.setStyleSheet("font-size:15px; font-weight:700;")
        head.addWidget(title)
        head.addStretch(1)
        self._s_badge = QLabel("待发送")
        self._s_badge.setObjectName("badge")
        head.addWidget(self._s_badge)
        right.addLayout(head)
        self._qr_label = QLabel("（待发送）")
        self._qr_label.setAlignment(Qt.AlignCenter)
        self._qr_label.setMinimumSize(400, 400)
        right.addWidget(self._qr_label, 1)
        lay.addWidget(right_w, 1)
        return tab

    def _build_recv_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)

        # ── 控制条：摄像头选择 + 刷新 + 码制 + 开始接收 ──
        ctrl = QFrame()
        ctrl.setObjectName("card")
        ctrl_row = QHBoxLayout(ctrl)
        ctrl_row.setContentsMargins(18, 10, 18, 10)
        ctrl_row.setSpacing(10)
        ctrl_row.addWidget(QLabel("摄像头"))
        self._r_cam = QComboBox()
        # 记录用户手动选择的设备索引；clear() 时索引变 -1 记为 None（下次默认第一项）
        self._r_cam.currentIndexChanged.connect(self._on_cam_selection_changed)
        self._r_cam.setMinimumWidth(220)
        ctrl_row.addWidget(self._r_cam, 1)
        self._cam_refresh_btn = QPushButton("刷新")
        self._cam_refresh_btn.setObjectName("secondary")
        self._icon_buttons.append((self._cam_refresh_btn, "refresh"))
        self._cam_refresh_btn.clicked.connect(self._refresh_camera_devices)
        self._cam_refresh_btn.setFixedWidth(92)
        ctrl_row.addWidget(self._cam_refresh_btn)
        ctrl_row.addSpacing(6)
        # 码制选择只在有多个码制时才有意义：彩色码暂不暴露后只剩标准 QR，
        # 整个标签 + 下拉一起隐藏（工具栏空间紧张，禁用态会更突兀）。
        self._r_codec_label = QLabel("码制")
        self._r_codec = QComboBox(); self._r_codec.addItems([n for n, _ in self.CODEC_OPTIONS])
        self._r_codec.setFixedWidth(158)
        for w in (self._r_codec_label, self._r_codec):
            ctrl_row.addWidget(w)
            w.setVisible(len(self.CODEC_OPTIONS) > 1)
        if len(self.CODEC_OPTIONS) > 1:
            ctrl_row.addSpacing(6)
        ctrl_row.addSpacing(6)
        self._r_start = QPushButton("开始接收")
        self._r_start.setCheckable(True)
        self._r_start.toggled.connect(self._toggle_recv)
        self._r_start.setMinimumHeight(42)
        self._r_start.setFixedWidth(136)
        ctrl_row.addWidget(self._r_start)
        lay.addWidget(ctrl)

        # ── 进度卡（进度条 + 缺块明细）：独立一行，位于预览/结果上方 ──
        prog_card = QFrame()
        prog_card.setObjectName("subcard")
        pc = QVBoxLayout(prog_card)
        pc.setContentsMargins(16, 10, 16, 10)
        pc.setSpacing(8)
        self._r_progress = QProgressBar(); self._r_progress.setValue(0); pc.addWidget(self._r_progress)
        # 缺块明细：传输中展示仍未恢复的源块索引，缺 0 块或未开始时隐藏
        self._r_missing = QLabel("")
        self._r_missing.setStyleSheet("color:#DC2626; font-size:12px;")
        self._r_missing.setWordWrap(True)
        self._r_missing.hide()
        pc.addWidget(self._r_missing)
        lay.addWidget(prog_card)

        # ── 相机预览 + 接收结果 同行（3:1）：预览区获得充足宽度，不再被纵向挤压 ──
        row = QHBoxLayout()
        row.setSpacing(12)

        # 相机预览卡（占 3 份宽）
        preview = QFrame()
        preview.setObjectName("card")
        pv = QVBoxLayout(preview)
        pv.setContentsMargins(20, 12, 20, 14)
        pv.setSpacing(8)
        phead = QHBoxLayout()
        ptitle = QLabel("相机预览")
        ptitle.setStyleSheet("font-size:15px; font-weight:700;")
        phead.addWidget(ptitle)
        # 采集实况（常驻，不随状态栏消息被覆盖）：分辨率/帧率/编码 + 真实出帧 + 解码耗时
        self._cam_meta = QLabel("")
        self._cam_meta.setStyleSheet("color:#64748B; font-size:11px;")
        self._cam_meta.setToolTip(
            "摄像头实际生效的采集参数。\n"
            "识别不稳时先看这里：分辨率越低、码点每个模块分到的像素越少。")
        phead.addWidget(self._cam_meta)
        phead.addStretch(1)
        self._r_badge = QLabel("等待接收")
        self._r_badge.setObjectName("badge")
        phead.addWidget(self._r_badge)
        # 保存当前画面（诊断用）：把相机原始帧存成 PNG，便于离线定位识别问题。
        # 放在徽标右侧、用次要按钮样式——原先夹在实况文字与徽标之间、且继承主按钮
        # 蓝底填充，24px 高把文字压得看不清，也会打断「接收中」这个状态标识的阅读。
        self._r_dump = QPushButton("存帧")
        self._r_dump.setObjectName("mini")
        self._r_dump.setToolTip("把当前相机画面存为 PNG（排查识别问题用）")
        self._r_dump.setMinimumWidth(64)
        self._r_dump.setFixedHeight(28)
        self._r_dump.clicked.connect(self._dump_frame)
        self._r_dump.setVisible(False)
        phead.addWidget(self._r_dump)
        pv.addLayout(phead)
        self._cam_label = QLabel("（摄像头预览）")
        self._cam_label.setAlignment(Qt.AlignCenter)
        self._cam_label.setMinimumSize(480, 180)
        pv.addWidget(self._cam_label, 1)
        # 卡住提示：能出画面、会话也建立了，但长时间 0 进展时给出可执行建议。
        # 只「干等」是最糟的体验——用户无法判断是没对准、帧长太大还是设备问题。
        self._r_hint = QLabel("")
        self._r_hint.setWordWrap(True)
        self._r_hint.setStyleSheet("color:#B45309; font-size:12px;")
        self._r_hint.setVisible(False)
        pv.addWidget(self._r_hint)
        row.addWidget(preview, 3)

        # 接收结果卡（窄列占 1 份宽：标题 + 纵向统计 + 结果区 + 复制/保存）
        result_card = QFrame()
        result_card.setObjectName("card")
        rc = QVBoxLayout(result_card)
        rc.setContentsMargins(14, 12, 14, 14)
        rc.setSpacing(8)
        rhead = QHBoxLayout()
        rtitle = QLabel("接收结果")
        rtitle.setStyleSheet("font-size:15px; font-weight:700;")
        rhead.addWidget(rtitle)
        rhead.addStretch(1)
        rc.addLayout(rhead)
        # 链路统计：窄列下双端纵向堆叠（左=已收/丢弃/未识别/已用，右=速率/fps/ETA）
        self._r_stats = QWidget()
        stats_col = QVBoxLayout(self._r_stats)
        stats_col.setContentsMargins(0, 0, 0, 0)
        stats_col.setSpacing(4)
        _stat_style = "color:#64748B; font-size:12px;"
        self._r_stats_left = QLabel(""); self._r_stats_left.setStyleSheet(_stat_style)
        self._r_stats_left.setWordWrap(True)
        self._r_stats_right = QLabel(""); self._r_stats_right.setStyleSheet(_stat_style)
        self._r_stats_right.setWordWrap(True)
        stats_col.addWidget(self._r_stats_left)
        stats_col.addWidget(self._r_stats_right)
        self._r_stats.hide()
        rc.addWidget(self._r_stats)
        self._r_result = QTextEdit(); self._r_result.setReadOnly(True)
        self._r_result.setPlaceholderText("接收完成后，文本可复制；文件可选择路径另存。")
        rc.addWidget(self._r_result, 1)
        # 字符统计角标（框内右下角）：当前字符数 / 应接收字符数
        self._r_count = QLabel("0 字符", self._r_result)
        self._r_count.setStyleSheet("color:#64748B; background:rgba(255,255,255,0.85); padding:1px 6px; border-radius:6px; font-size:11px;")
        self._r_result.viewport().installEventFilter(self)
        copy_row = QHBoxLayout(); copy_row.addStretch(1)
        self._r_copy = QPushButton("复制文本"); self._r_copy.clicked.connect(self._copy_result)
        self._r_copy.setObjectName("secondary")
        self._r_copy.setStyleSheet("padding:8px 10px;")   # 窄列内紧凑内边距
        self._icon_buttons.append((self._r_copy, "copy"))
        self._r_copy.hide()
        copy_row.addWidget(self._r_copy)
        self._r_save = QPushButton("保存文件"); self._r_save.clicked.connect(self._save_received_file)
        self._r_save.setIcon(_svg_icon(_ICON_SVGS["save"], "#FFFFFF", 16))
        self._r_save.setIconSize(QSize(16, 16))
        self._r_save.setStyleSheet("padding:8px 10px;")
        self._r_save.hide()
        copy_row.addWidget(self._r_save)
        rc.addLayout(copy_row)
        row.addWidget(result_card, 1)

        lay.addLayout(row, 1)
        # 所有控件就绪后再启动首次后台探测（探测期间会禁用下拉/开始/刷新）
        self._refresh_camera_devices()
        return tab

    # ────────────── 字符统计角标 ──────────────
    def _update_s_count(self):
        if not hasattr(self, "_s_count"):
            return
        self._s_count.setText(f"{len(self._s_textedit.toPlainText())} 字符")
        self._s_count.adjustSize()
        vp = self._s_textedit.viewport()
        self._s_count.move(vp.width() - self._s_count.width() - 10, vp.height() - self._s_count.height() - 8)
        self._s_count.raise_()

    def _update_r_count(self):
        if not hasattr(self, "_r_count"):
            return
        pipe = self._pipe
        if pipe is not None and pipe.result is not None:
            # 已完成：文本按字符数（与发送端口径一致），文件按字节数
            r = pipe.result
            label = (f"{len(self._r_result.toPlainText())} 字符"
                     if r.content_type == ContentType.TEXT else f"{len(r.data)} 字节")
        elif pipe is not None and pipe.session.manifest is not None:
            # 传输中：文本整包解码后才产生字符数，过程中只能给块恢复进度
            m = pipe.session.manifest
            label = f"{round(pipe.progress * m.total_chunks)}/{m.total_chunks} 块"
        else:
            label = "0 字符"
        self._r_count.setText(label)
        self._r_count.adjustSize()
        vp = self._r_result.viewport()
        self._r_count.move(vp.width() - self._r_count.width() - 10, vp.height() - self._r_count.height() - 8)
        self._r_count.raise_()

    def _update_r_stats(self):
        """刷新链路统计（单行双端）：左=已收/丢弃/未识别/已用，右=速率/fps/ETA。"""
        if not hasattr(self, "_r_stats"):
            return
        pipe = self._pipe
        if pipe is None or pipe.session.manifest is None:
            self._r_stats.hide()
            self._r_stats_left.setText("")
            self._r_stats_right.setText("")
            return
        elapsed = max(1e-6, pipe.elapsed_seconds or 0.0)
        eff = pipe.effective_bps / 1024.0
        eta = pipe.eta_seconds
        eta_txt = "—" if eta is None else _format_eta(eta)
        right = f"有效 {eff:.1f} KB/s | 帧率 {pipe.fps_instant:.1f} | 剩余 {eta_txt}"
        left = (f"已收 {pipe.valid_data_frames} | 丢弃 {pipe.bad_frames} "
                f"({pipe.drop_rate:.1%}) | 未识别 {pipe.missed_images} | 已用 {elapsed:.1f}s")
        self._r_stats_left.setText(left)
        self._r_stats_right.setText(right)
        self._r_stats.show()
        self._refresh_cam_meta()   # 每模块像素随取景变化，跟着 500ms 统计一起刷
        self._check_stalled(pipe)

    def _module_pixels(self) -> tuple[float, int] | None:
        """实测「每个 QR 模块分到几个相机像素」，返回 (px/模块, 模块数) 或 None。

        依据：backend 记录最近一次成功解码的 (码宽px, 图宽px, 字节数)，结合当前会话
        源块大小推出的模块数。这是判断识别瓶颈的唯一客观量——取景越小、帧长越大，
        每模块像素越少，越到临界值以下就整帧解不出。
        """
        pipe = self._pipe
        backend = getattr(pipe, "backend", None) if pipe is not None else None
        geo = getattr(backend, "last_code_geometry", None)
        if not geo:
            return None
        code_w, _img_w, nbytes = geo
        modules = _qr_modules_for_frame_bytes(nbytes)
        if not modules:
            return None
        return code_w / modules, modules

    def _check_stalled(self, pipe) -> None:
        """会话已建立但长时间零进展 → 按实测模块像素给出**针对性**建议。

        两类成因的处置完全不同，给错建议等于让用户白折腾：
        - 码在画面里太小（取景不足）→ 让用户靠近/放大，像素立刻变多，免费提速；
        - 码够大但仍扫不出 → 才是帧长太大（模块太密），需要降帧长。
        """
        if not hasattr(self, "_r_hint"):
            return
        # 新一批码（session_id 变了）：卡住计时必须重来，否则上一轮攒下的「已卡住」
        # 会立刻把提示弹到新传输上，用户会以为新文件也卡了。
        sid = getattr(getattr(pipe, "session", None), "session_id", None)
        if sid != self._hint_session_id:
            self._hint_session_id = sid
            self._stall_since = None
            self._r_hint.setVisible(False)
        if pipe.is_complete or pipe.progress > 0:
            self._stall_since = None
            self._r_hint.setVisible(False)
            return
        now = time.monotonic()
        if self._stall_since is None:
            self._stall_since = now
            return
        if now - self._stall_since < _STALL_SECONDS:
            return
        chunk = 1 << pipe.session.manifest.chunk_size_log
        measured = self._module_pixels()
        if measured is not None and measured[0] < _GOOD_MODULE_PX:
            self._r_hint.setText(
                f"二维码在画面里偏小（每模块约 {measured[0]:.1f} 像素，需 ≥{_GOOD_MODULE_PX:.0f}）。"
                "把手机靠近、或让二维码占满预览画面的一半以上，速度会明显变快。")
        else:
            px = f"（每模块约 {measured[0]:.1f} 像素）" if measured else ""
            self._r_hint.setText(
                f"画面里能对上码，但 {_STALL_SECONDS} 秒没解出一个数据帧{px}"
                f"（当前源块 {chunk}B）。码点太密：请把发送端「帧长」调到 700B。")
        self._r_hint.setVisible(True)

    def _update_r_missing(self):
        """刷新缺块明细：传输中展示未恢复源块索引，缺 0 块或已完成时隐藏。"""
        if not hasattr(self, "_r_missing"):
            return
        pipe = self._pipe
        missing = pipe.missing_indices if (pipe is not None and not pipe.is_complete) else []
        if not missing:
            self._r_missing.hide()
            self._r_missing.setText("")
            return
        shown = missing[:20]
        suffix = " …" if len(missing) > 20 else ""
        self._r_missing.setText(f"缺 {len(missing)} 块: {shown}{suffix}")
        self._r_missing.show()

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent

        t = event.type()
        # 窗口控制按钮 hover 高亮：关闭按钮悬停时图标换白（红底上清晰），其余换主色
        if obj is self._win_min or obj is self._win_max or obj is self._win_close:
            if t == QEvent.HoverEnter:
                if obj is self._win_close:
                    obj.setIcon(_svg_icon(_ICON_SVGS["winclose"], "#FFFFFF", 14))
                else:
                    accent = "#3B82F6" if self._dark else "#2563EB"
                    name = "winmin" if obj is self._win_min else (
                        "winrestore" if self._maximized else "winmax")
                    obj.setIcon(_svg_icon(_ICON_SVGS[name], accent, 14))
            elif t == QEvent.HoverLeave:
                fg = "#E2E8F0" if self._dark else "#0F172A"
                name = "winmin" if obj is self._win_min else (
                    "winrestore" if self._maximized else "winmax")
                if obj is self._win_close:
                    name = "winclose"
                obj.setIcon(_svg_icon(_ICON_SVGS[name], fg, 14))
        if t == QEvent.Resize:
            if hasattr(self, "_s_textedit") and obj is self._s_textedit.viewport():
                self._update_s_count()
            elif hasattr(self, "_r_result") and obj is self._r_result.viewport():
                self._update_r_count()
        return super().eventFilter(obj, event)

    # ────────────── 无边框窗口：标题栏拖拽 + 边缘缩放 + 双击最大化 ──────────────
    _RESIZE_EDGE = 8   # 边缘命中宽度（px）

    def _in_titlebar(self, pos) -> bool:
        """窗口坐标点是否落在顶栏（标题栏）区域内，且不在可交互控件上。"""
        tb = getattr(self, "_topbar", None)
        return tb is not None and tb.geometry().contains(pos)

    def _hit_edge(self, pos):
        """窗口坐标 → 缩放方向码（'n','s','e','w' 组合，如 'nw'）；未命中返回 None。

        仅命中窗口边缘（_RESIZE_EDGE 内）。布局 margins ≥ 16，边缘区无子控件干扰。
        """
        r = self.rect()
        e = self._RESIZE_EDGE
        x, y = pos.x(), pos.y()
        left = x <= e
        right = x >= r.width() - 1 - e
        top = y <= e
        bottom = y >= r.height() - 1 - e
        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        if left:
            return "w"
        if right:
            return "e"
        if top:
            return "n"
        if bottom:
            return "s"
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self._maximized:
            pos = e.position().toPoint()
            edge = self._hit_edge(pos)
            if edge:
                # 边缘缩放：记录起点几何与全局坐标，移动时按方向调整
                self._resize_edge = edge
                self._resize_start = (self.geometry(), e.globalPosition().toPoint())
                e.accept()
                return
            if self._in_titlebar(pos):
                self._drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._resize_edge is not None and (e.buttons() & Qt.LeftButton):
            self._apply_resize(e.globalPosition().toPoint())
            e.accept()
            return
        if self._drag_off is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_off)
            e.accept()
            return
        # 未按下：边缘处更新缩放光标（Windows 由 nativeEvent/WM_NCHITTEST 自动处理）
        if not self._maximized and e.buttons() == Qt.NoButton:
            self._update_edge_cursor(e.position().toPoint())
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._resize_edge = None
        self._resize_start = None
        self._drag_off = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and self._in_titlebar(e.position().toPoint()):
            self._toggle_maximize()
            return
        super().mouseDoubleClickEvent(e)

    def _apply_resize(self, gpos):
        """按记录的方向码调整窗口几何（左/上边拖动改 left/top，右/下改 right/bottom）。"""
        geom, start = self._resize_start
        g = QRect(geom)
        dx = gpos.x() - start.x()
        dy = gpos.y() - start.y()
        edge = self._resize_edge
        if "w" in edge:
            g.setLeft(g.left() + dx)
        if "e" in edge:
            g.setRight(g.right() + dx)
        if "n" in edge:
            g.setTop(g.top() + dy)
        if "s" in edge:
            g.setBottom(g.bottom() + dy)
        # 不小于布局最小尺寸（防止内容被压爆）
        minsz = self.minimumSizeHint()
        g.setWidth(max(g.width(), minsz.width()))
        g.setHeight(max(g.height(), minsz.height()))
        self.setGeometry(g)

    def _update_edge_cursor(self, pos):
        cur = _EDGE_CURSORS[self._hit_edge(pos)]
        if self.cursor().shape() != cur:
            self.setCursor(cur)

    def _phys_to_logical(self, x: int, y: int) -> tuple[float, float]:
        """物理像素屏幕坐标 → 逻辑（设备无关）像素坐标。

        WM_NCHITTEST 的 lParam 是物理像素，而 Qt 的 frameGeometry()/geometry()
        是逻辑像素。高 DPI（125%/150%/200%）下若直接比较，右/下「边缘」会按
        缩放比放大成整片死区（光标变左右箭头、按钮点击被系统当窗口缩放吞掉），
        左/上边缘则缩没。逐屏换算兼容多显示器混合 DPI。
        """
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QGuiApplication

        for scr in QGuiApplication.screens():
            sg = scr.geometry()                      # 逻辑几何
            dpr = scr.devicePixelRatio()
            phys = QRect(round(sg.left() * dpr), round(sg.top() * dpr),
                         round(sg.width() * dpr), round(sg.height() * dpr))
            if phys.contains(x, y):
                return sg.left() + (x - phys.left()) / dpr, \
                       sg.top() + (y - phys.top()) / dpr
        return float(x), float(y)   # 未命中任何屏幕（异常情况）→ 原样返回

    def nativeEvent(self, eventType, message):
        """Windows：单实例「显示窗口」消息 + WM_NCHITTEST 交还系统处理边缘缩放。

        第二实例经 PostMessage 发来的注册消息（QR_FERRY_SHOW_WINDOW）→ 从托盘
        恢复窗口；WM_NCHITTEST 命中边缘返回 HT* 码让 Windows 接管（原生光标/
        动画/snap），未命中走 Qt 事件（拖拽标题栏等）。
        """
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                show_msg = getattr(self, "_show_win_msg", None)
                if show_msg is not None and msg.message == show_msg:
                    # 单实例：用户再次双击 exe，把（可能已最小化到托盘的）窗口恢复
                    self._show_from_tray()
                    return True, 0
                if not self._maximized and msg.message == 0x0084:   # WM_NCHITTEST
                    x = ctypes.c_short(msg.lParam & 0xFFFF).value
                    y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                    # WM_NCHITTEST 的 lParam 是物理像素屏幕坐标，而 frameGeometry()
                    # 是逻辑像素：高 DPI（125%/150%/200%）下不换算，右/下「边缘」
                    # 会按缩放比放大成整片死区——光标变左右箭头、按钮点击被 Windows
                    # 当作窗口缩放吞掉（接收页右侧按钮、顶栏控制按钮点不动）。
                    x, y = self._phys_to_logical(x, y)
                    r = self.frameGeometry()
                    e = self._RESIZE_EDGE
                    left = x <= r.left() + e
                    right = x >= r.right() - e
                    top = y <= r.top() + e
                    bottom = y >= r.bottom() - e
                    ht = None
                    if top and left:
                        ht = 13      # HTTOPLEFT
                    elif top and right:
                        ht = 14      # HTTOPRIGHT
                    elif bottom and left:
                        ht = 16      # HTBOTTOMLEFT
                    elif bottom and right:
                        ht = 17      # HTBOTTOMRIGHT
                    elif left:
                        ht = 10      # HTLEFT
                    elif right:
                        ht = 11      # HTRIGHT
                    elif top:
                        ht = 12      # HTTOP
                    elif bottom:
                        ht = 15      # HTBOTTOM
                    if ht is not None:
                        return True, ht
            except Exception:   # noqa: BLE001, S110 — nativeEvent 失败静默回退 Qt 逻辑
                pass
        return super().nativeEvent(eventType, message)

    # ────────────── 发送 ──────────────
    def _selected_backend(self, combo: QComboBox) -> StandardQrBackend | ColorMatrixBackend:
        _, code = self.CODEC_OPTIONS[combo.currentIndex()]
        return ColorMatrixBackend() if code == "color" else StandardQrBackend()

    def _sync_send_codec_controls(self) -> None:
        # 彩色码已从界面撤下（CODEC_OPTIONS 仅剩标准 QR），is_color 恒为 False，
        # 下方彩色码分支目前不可达；保留是为了恢复彩色码时无需重写这段联动逻辑。
        is_color = self._s_codec.currentIndex() == 1
        self._s_grid.setEnabled(not is_color)
        if is_color and self._s_grid.currentIndex() != 0:
            self._s_grid.setCurrentIndex(0)
        # 彩色码帧长固定为 COLOR_MATRIX_MAX_FRAME_BYTES，帧长下拉同步禁用并复位
        self._s_frame_bytes.setEnabled(not is_color)
        if is_color and self._s_frame_bytes.currentText() != "1200":
            self._s_frame_bytes.setCurrentText("1200")

    def _on_send_mode(self):
        is_file = self._s_file.isChecked()
        self._s_pick.setVisible(is_file)
        self._s_filelabel.setVisible(is_file)
        if hasattr(self, "_s_drop_hint"):
            self._s_drop_hint.setVisible(is_file)
        self._s_optimize.setVisible(is_file)
        self._s_textedit.setVisible(not is_file)
        if hasattr(self, "_s_clear"):
            self._s_clear.setVisible(not is_file)

    def _update_optimize_preview(self):
        """选文件/切换有损选项时预览：显示将实际发送的优化结果（无损自动，有损按勾选）。"""
        if not hasattr(self, "_s_optimize"):
            return
        path = getattr(self, "_file_path", None)
        if not (self._s_file.isChecked() and path):
            return
        res = optimize_file(path, lossy=self._s_optimize.isChecked())
        if res is not None:
            name = os.path.basename(path)
            self._s_filelabel.setFullText(f"{name} → {res.filename}（{res.description}）")
            self._s_filelabel.setToolTip(res.description)
        else:
            name = os.path.basename(path)
            self._s_filelabel.setFullText(name)
            self._s_filelabel.setToolTip(path)

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if path:
            self._load_send_file(path)

    # ────────────── 文件拖拽 ──────────────
    @staticmethod
    def _dropped_file(mime) -> str | None:
        """从拖拽数据取第一个存在的本地文件；无则返回 None（目录/文本/网络 URL 一律忽略）。"""
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                if path and os.path.isfile(path):
                    return path
        return None

    def _send_busy(self) -> bool:
        """发送中不接受换文件：控制器已按旧文件建好会话，中途换源会让进度语义错乱。"""
        return self._send_timer.isActive()

    def _set_drag_hint(self, active: bool) -> None:
        if not hasattr(self, "_s_filelabel"):
            return
        if active:
            self._s_filelabel.setFullText("松开鼠标载入文件…")
            self._s_filelabel.setToolTip("拖入的文件将作为发送内容")
            return
        path = getattr(self, "_file_path", "")
        self._s_filelabel.setFullText(os.path.basename(path) if path else "（未选择）")
        self._s_filelabel.setToolTip(path or "（未选择）")
        self._update_optimize_preview()

    def dragEnterEvent(self, event):
        if self._send_busy() or self._dropped_file(event.mimeData()) is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._set_drag_hint(True)
        self.statusBar().showMessage("松开鼠标载入文件…")

    def dragMoveEvent(self, event):
        if self._send_busy() or self._dropped_file(event.mimeData()) is None:
            event.ignore()
            return
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_drag_hint(False)
        self.statusBar().showMessage("就绪")
        event.accept()

    def dropEvent(self, event):
        path = self._dropped_file(event.mimeData())
        if path is None or self._send_busy():
            self._set_drag_hint(False)
            event.ignore()
            return
        event.acceptProposedAction()
        self._load_send_file(path)

    def _load_send_file(self, path: str) -> None:
        """载入发送文件（选择按钮与拖拽共用）：切到发送页 / 文件模式并刷新预览。"""
        if self.tabs.currentIndex() != 0:
            self._tab_send.click()      # 同步顶部 tab 按钮态（不然只有页面切换）
        if not self._s_file.isChecked():
            self._s_file.setChecked(True)   # 触发 _on_send_mode，显示文件行
        self._file_path = path
        name = os.path.basename(path)
        self._s_filelabel.setFullText(name)
        self._s_filelabel.setToolTip(path)
        self._update_optimize_preview()
        self.statusBar().showMessage(f"已载入文件：{name}")

    def _toggle_send(self, checked: bool):
        if checked:
            self._start_send()
        else:
            self._stop_send()

    def _reset_send_stats(self) -> None:
        """清零发送速率统计，开始一个新的统计周期。

        两个发送入口（_start_send / _resend）启动前统一调用：
        _sent_frames/_sent_bytes 若不在此清零，会跨会话残留上次累计值，
        首个 tick 用大累计数除以微小 elapsed 算出天文速率，多次发送后计时/速率错乱。
        """
        self._send_start_ts = None
        self._send_block_size = 0
        self._sent_frames = 0
        self._sent_bytes = 0

    def _probe_resume_send(self) -> SendController | None:
        """开始发送前探测未完成的发送会话；内容与当前输入一致时询问是否断点续传。

        与接收端 _probe_resume 对称：恢复入口并入主操作按钮而非独立按钮。
        返回需恢复的 SendController；None 表示无快照 / 内容不一致 / 用户选择新会话。
        选择新会话会清除旧快照，避免下次重复询问（与接收端拒绝续传语义一致）。
        """
        from qrferry.app import session_store
        try:
            ctrl = session_store.load_sender(_SENDER_STATE_DIR)
        except OSError:
            ctrl = None
        if ctrl is None:
            return None
        if not self._sender_matches_input(ctrl):
            return None
        kind = "文件" if ctrl.source_kind == "file" else "文本"
        msg = (f"检测到上次未完成的发送（会话 {ctrl.session_id}，{kind}，K={ctrl.K}，"
               f"已发 {ctrl.next_sid} 个符号）：\n"
               f"是否从上次中断处继续？选择“否”将开始全新会话（接收端需从头扫描）。")
        choice = QMessageBox.question(
            self, "断点续传", msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if choice == QMessageBox.Yes:
            return ctrl
        session_store.clear_sender(_SENDER_STATE_DIR)
        return None

    def _sender_matches_input(self, ctrl: SendController) -> bool:
        """发送端快照内容与当前界面输入是否一致（一致才有续传价值，避免无关弹窗）。"""
        if ctrl.source_kind == "text":
            if self._s_file.isChecked():
                return False
            return self._s_textedit.toPlainText().encode("utf-8") == ctrl.raw_data
        if ctrl.source_kind == "file":
            if not self._s_file.isChecked() or not self._file_path:
                return False
            if ctrl.optimized:
                # 快照数据是优化产物（.opt sidecar），无法与源文件直接比对 → 不询问
                return False
            # load_sender 已校验快照数据与源文件 SHA 一致，此处路径一致即可认为内容一致
            return self._file_path == ctrl.source_path
        return False

    def _start_send(self):
        resumed = self._probe_resume_send()
        if resumed is not None:
            self._send_ctrl = resumed
            self._backend = self._selected_backend(self._s_codec)
            self._send_iter = resumed.infinite_frames()
            self._reset_send_stats()
            self._s_start.setText("停止发送")
            self._update_running_icons()
            self._s_badge.setText(f"恢复发送 · K={resumed.K}")
            self._send_timer.start(int(1000 / self._s_fps.value()))
            # 快照游标随续传推进而更新，支持多次分段续传（失败不阻断发送）
            from qrferry.app import session_store
            try:
                session_store.save_sender(resumed, _SENDER_STATE_DIR)
            except OSError:
                pass
            self.statusBar().showMessage(
                f"恢复发送：会话 {resumed.session_id}，从 symbol_id={resumed.next_sid} 继续（K={resumed.K}）")
            return
        if self._s_file.isChecked():
            if not self._file_path or not os.path.isfile(self._file_path):
                QMessageBox.warning(self, "提示", "请先选择有效文件")
                self._s_start.setChecked(False)
                return
            with open(self._file_path, "rb") as f:
                data = f.read()
            filename = os.path.basename(self._file_path)
            ct = ContentType.FILE
            opt_note = None
            # 无损优化自动执行（内容不变）；有损转码仅在用户勾选时允许
            res = optimize_file(self._file_path, lossy=self._s_optimize.isChecked())
            if res is not None:
                data, filename, opt_note = res.data, res.filename, res.description
        else:
            text = self._s_textedit.toPlainText()
            if not text:
                QMessageBox.warning(self, "提示", "请输入文本")
                self._s_start.setChecked(False)
                return
            data = text.encode("utf-8")
            filename = ""
            ct = ContentType.TEXT
            opt_note = None
        self._backend = self._selected_backend(self._s_codec)
        is_color = isinstance(self._backend, ColorMatrixBackend)
        cfg = SenderConfig(
            chunk_size_log=(COLOR_MATRIX_CHUNK_SIZE_LOG if is_color
                            else chunk_size_log_for_frame_bytes(
                                int(self._s_frame_bytes.currentText()))),
            ecc_level=self._s_ecc.currentText(),
            grid=(1, 1) if is_color else self.GRID_OPTIONS[self._s_grid.currentIndex()][1],
            rounds=3,
            max_frame_bytes=(COLOR_MATRIX_MAX_FRAME_BYTES if is_color
                             else int(self._s_frame_bytes.currentText())),
            redundancy=float(self._s_redundancy.currentText()),
            manifest_interval=8 if is_color else 32,
        )
        sid = random.randint(1, 0xFFFFFFFF)
        is_file = (ct == ContentType.FILE)
        self._send_ctrl = SendController(
            data, ct, filename, sid, cfg,
            source_kind="file" if is_file else "text",
            source_path=self._file_path if is_file else None,
            optimized=opt_note is not None)
        self._send_iter = self._send_ctrl.infinite_frames()
        self._reset_send_stats()
        self._s_start.setText("停止发送")
        self._update_running_icons()
        self._s_badge.setText(f"发送中 · K={self._send_ctrl.K}")
        self._send_timer.start(int(1000 / self._s_fps.value()))
        mode_desc = "持续流"
        c = self._send_ctrl
        enc = len(c._encoded)
        if c.config.compression == 0 or enc >= c.raw_size:
            comp_txt = f"压缩 {compression_name(c.config.compression)}（无收益）"
        else:
            comp_txt = (f"压缩 {compression_name(c.config.compression)} "
                        f"{c.raw_size}→{enc} 字节（省 {100 * (1 - enc / c.raw_size):.0f}%）")
        opt_txt = f"；{opt_note}" if opt_note else ""
        self.statusBar().showMessage(f"发送中：K={c.K}，{comp_txt}{opt_txt}，{mode_desc}")
        # 持久化发送端快照，支持断点续传（失败不阻断发送）
        from qrferry.app import session_store
        try:
            session_store.save_sender(self._send_ctrl, _SENDER_STATE_DIR)
        except OSError:
            pass

    def _stop_send(self):
        self._send_timer.stop()
        if self._s_start.isChecked():
            self._s_start.setChecked(False)
        self._s_start.setText("开始发送")
        self._update_running_icons()
        self._s_badge.setText("待发送")
        self._send_start_ts = None   # 重置速率统计，下次发送重新计时

    def _resend(self):
        """人工补发：从当前游标追加 N 个新 DATA 符号（LT 喷泉码靠新符号补缺块，非重发旧帧）。"""
        if self._send_ctrl is None:
            QMessageBox.warning(self, "提示", "请先开始一次发送以建立会话")
            return
        n = self._s_extra.value()
        if n <= 0:
            return
        self._send_iter = self._send_ctrl.extra_data_frames(n)
        self._reset_send_stats()
        self._s_start.setText("停止发送")
        self._s_start.setChecked(True)
        self._update_running_icons()
        self._send_timer.start(int(1000 / self._s_fps.value()))
        self.statusBar().showMessage(
            f"补发 {n} 个新符号（自 symbol_id={self._send_ctrl.next_sid} 起）")

    def _tick_send(self):
        if self._send_iter is None or self._send_ctrl is None:
            self._stop_send()
            return
        cells = self._send_ctrl.grid_cells
        frames = []
        for _ in range(cells):
            try:
                frames.append(next(self._send_iter))
            except StopIteration:
                self._stop_send()
                self.statusBar().showMessage("发送完成（已播放全部轮数）")
                return
        ecc = self._send_ctrl.config.ecc_level
        if cells == 1:
            img = self._backend.encode(frames[0], ecc_level=ecc)
        else:
            imgs = [self._backend.encode(f, ecc_level=ecc, box_size=6, border=2) for f in frames]
            img = self._compose_grid(imgs)
        pix = QPixmap.fromImage(_qimage_from_pil(img))
        # QR 是离散黑白模块矩阵，必须最近邻（Fast）保留方块锐利；双线性（Smooth）会在模块
        # 边缘产生灰色过渡，高密度帧（zip 满载）缩放后易致接收端 zxing 误读 → 重组数据损坏。
        self._qr_label.setPixmap(pix.scaled(self._qr_label.size(), Qt.KeepAspectRatio, Qt.FastTransformation))
        # 发送速率统计（懒初始化：兼容 _start_send / _resend 两入口）
        if self._send_start_ts is None:
            self._send_start_ts = time.monotonic()
            self._send_block_size = 1 << self._send_ctrl.config.chunk_size_log
        self._sent_frames += cells
        self._sent_bytes += cells * self._send_block_size
        elapsed = max(1e-6, time.monotonic() - self._send_start_ts)
        self.statusBar().showMessage(
            f"发送中：K={self._send_ctrl.K} · {self._sent_bytes / elapsed / 1024:.1f} KB/s · "
            f"{self._sent_frames / elapsed:.1f} fps · 已用 {elapsed:.1f}s")

    def _compose_grid(self, imgs):
        rows, cols = self._send_ctrl.config.grid
        cw = max(im.width for im in imgs)
        ch = max(im.height for im in imgs)
        canvas = Image.new("L", (cols * cw, rows * ch), 255)
        for i, im in enumerate(imgs):
            r, c = divmod(i, cols)
            canvas.paste(im, (c * cw, r * ch))
        return canvas

    # ────────────── 接收 ──────────────
    def _refresh_camera_devices(self):
        """后台真实探测摄像头：逐个试开，只保留可用设备；期间禁用相关控件。"""
        self._r_cam.clear()
        self._r_cam.addItem("正在探测摄像头…", -1)
        self._r_cam.setEnabled(False)
        self._r_start.setEnabled(False)
        self._cam_refresh_btn.setEnabled(False)
        threading.Thread(
            target=self._camera_probe_worker, name="cam-probe", daemon=True).start()

    def _camera_probe_worker(self):
        """探测线程：优先子进程全新枚举（热插拔设备可见），失败回退进程内。

        OpenCV 的 Windows 设备枚举在进程内有缓存：应用启动后才插入的外接
        摄像头，主进程内怎么探测都看不见。子进程每次都是全新枚举，天然免疫。
        子进程失败（超时/打包异常等）回退进程内探测——老行为兜底。
        探测异常也必须 emit：否则下拉框卡在「正在探测摄像头…」且开始按钮
        永远禁用——回退空列表由 UI 侧落兜底项。
        """
        devices = probe_available_cameras_fresh(max_index=9)
        if devices is None:
            try:
                devices = probe_available_cameras(max_index=9)
            except Exception:  # noqa: BLE001 — 探测是尽力而为，任何异常都不允许卡死 UI
                devices = []
        self._camera_probe_done.emit(devices)

    def _on_cam_selection_changed(self, _index: int) -> None:
        """记录用户选择的设备索引；仅真实设备（data>=0）更新，占位/清空不抹旧值。"""
        data = self._r_cam.currentData()
        if data is not None and int(data) >= 0:
            self._cam_selected_idx = int(data)

    def _on_camera_probe_done(self, devices):
        # 先保存选择：下方 clear()/addItem 会触发 handler 改写 _cam_selected_idx
        sel = self._cam_selected_idx
        self._r_cam.clear()
        backends: dict[int, str] = {}
        if devices:
            for device in devices:
                self._r_cam.addItem(device.label, device.index)
                backends[device.index] = device.backend
            # 刷新后恢复用户上次的选择；未选过或原设备已不可用则回退第一项
            if sel is not None:
                found = self._r_cam.findData(sel)
                self._r_cam.setCurrentIndex(max(found, 0))
            else:
                self._r_cam.setCurrentIndex(0)
        else:
            # 一个都探测不到时保留兜底项，行为与旧版一致
            self._r_cam.addItem("0 · Camera 0 (未打开)", 0)
            backends[0] = "auto"
        self._r_cam_backends = backends   # 记录每个索引的探测后端，供采集打开复用
        self._r_cam.setEnabled(True)
        self._r_start.setEnabled(True)
        self._cam_refresh_btn.setEnabled(True)

    def _on_device_in_use(self, idx: int):
        """黑屏顺延后同步下拉框到实际出画面的设备（触发 handler 记录新选择）。"""
        found = self._r_cam.findData(idx)
        if found >= 0:
            self._r_cam.setCurrentIndex(found)

    def _toggle_recv(self, checked: bool):
        if checked:
            self._start_recv()
        else:
            self._stop_recv()

    def _probe_resume(self) -> ReceiveSession | None:
        """探测内部状态目录下的未完成会话；有则询问用户是否恢复。"""
        from qrferry.app import session_store
        sess = session_store.load(_RECEIVER_STATE_ROOT)
        if sess is None or sess.is_complete:
            return None
        missing = len(sess.missing_indices)
        total = sess.K
        recovered = total - missing
        msg = (f"检测到未完成的接收（会话 {sess.session_id}）：\n"
               f"已恢复 {recovered}/{total} 块（{sess.progress:.0%}），"
               f"剩 {missing} 块待补齐。\n是否继续接收？")
        choice = QMessageBox.question(
            self, "断点续传", msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if choice == QMessageBox.Yes:
            return sess
        session_store.clear(_RECEIVER_STATE_ROOT)
        return None

    def _start_recv(self):
        data = self._r_cam.currentData()
        cur = int(data) if data is not None and int(data) >= 0 else 0
        backends = getattr(self, "_r_cam_backends", {})
        # 候选设备：当前选中优先，其余按下拉框顺序——纯黑/打不开的设备自动顺延到
        # 下一个；同时携带每个索引的探测后端（热插拔设备常只有 DSHOW 可见，
        # 探测时已记下 backend，采集打开时优先复用同一后端，避免 MSMF 陈旧枚举）。
        candidates = [(cur, backends.get(cur, "auto"))]
        for i in range(self._r_cam.count()):
            d = self._r_cam.itemData(i)
            if d is not None and int(d) >= 0 and int(d) != cur:
                candidates.append((int(d), backends.get(int(d), "auto")))
        resumed = self._probe_resume()
        recv_backend = self._selected_backend(self._r_codec)
        self._pipe = ReceivePipeline(
            save_dir=_RECEIVER_STATE_ROOT, resume_from=resumed, backend=recv_backend)
        self._r_progress.setValue(0)
        self._r_result.clear()
        self._r_copy.hide()
        self._r_save.hide()
        self._update_r_count()
        self._update_r_missing()
        self._update_r_stats()
        self._r_start.setText("停止接收")
        self._update_running_icons()
        self._r_badge.setText("接收中")
        self._cam_refresh_btn.setEnabled(False)
        # 采集/解码都在工作线程，UI 仅启动线程后立即返回（open/read 在 _capture_loop 内）
        self._decode_queue = queue.Queue(maxsize=1)
        self._decode_stop.clear()
        self._capture_stop.clear()
        self._recv_generation += 1
        gen = self._recv_generation
        self._decode_restarts = 0
        self._last_decode_restart = 0.0
        self._capture_mode_idx = 0   # 每轮接收重新从最高分辨率试起（上次降档多半是临时带宽问题）
        # 诊断状态复位（含「保存画面」按钮可见性）
        self._cam_format = ""
        self._cam_actual = None
        self._capture_negotiated = None
        self._decode_ms = 0.0
        self._last_raw_frame = None
        self._fail_dumped = 0
        self._fail_streak = 0
        self._stall_since = None
        self._r_hint.setVisible(False)
        # 曝光寻优：每轮接收都重新做一次 A/B（光线条件可能变了）
        self._expo_state = "baseline"
        self._expo_base_rate = 0.0
        self._reset_expo_window()
        self._refresh_cam_meta()
        self._r_dump.setVisible(True)
        self._decode_thread = threading.Thread(
            target=self._decode_loop, args=(self._decode_queue, gen),
            name="recv-decode", daemon=True)
        self._decode_thread.start()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, args=(candidates, gen),
            name="recv-capture", daemon=True)
        self._capture_thread.start()
        self._r_stats_timer.start(500)   # 500ms 刷新实时速率/ETA，无需新帧到达
        self.statusBar().showMessage("正在打开摄像头…")
        self._cam_label.setText("正在打开摄像头…")   # 打开/顺延期间给出明确占位，避免误判黑屏

    def _stop_recv(self):
        self._r_stats_timer.stop()
        # 先停采集线程：置退出标记后短超时 join，cap 由 _capture_loop 退出时自行 release
        self._capture_stop.set()
        cthread = self._capture_thread
        if cthread is not None and cthread.is_alive():
            cthread.join(timeout=1.0)
        # join 超时（MSMF 慢读导致采集线程卡在 cap.read()）时兜底释放摄像头：
        # 否则打开的设备会拖住进程退出（cv2 原生采集线程不随解释器收尾终止）。
        if cthread is not None and cthread.is_alive():
            cap = getattr(self, "_cap", None)
            if cap is not None:
                try:
                    cap.release()
                except Exception:  # noqa: BLE001, S110 — 兜底释放尽力而为
                    pass
        self._capture_thread = None
        self._cap = None
        # 再停后台解码线程：残余帧由 daemon 线程自然丢弃
        self._decode_stop.set()
        dthread = self._decode_thread
        if dthread is not None and dthread.is_alive():
            dthread.join(timeout=0.5)
        self._decode_thread = None
        self._decode_queue = None
        if self._r_start.isChecked():
            self._r_start.setChecked(False)
        self._r_start.setText("开始接收")
        self._update_running_icons()
        self._r_badge.setText("等待接收")
        self._cam_refresh_btn.setEnabled(True)
        self._r_dump.setVisible(False)
        self._cam_format = ""
        self._cam_actual = None
        self._capture_negotiated = None
        self._decode_ms = 0.0
        self._refresh_cam_meta()
        # 全程没出过帧（黑屏/打开失败等）时恢复占位文字，避免残留"正在打开摄像头…"
        if self._cam_label.pixmap() is None:
            self._cam_label.setText("（摄像头预览）")

    def _decode_loop(self, q: queue.Queue, gen: int):
        """后台解码线程：只取槽位里的最新帧，慢解码时旧帧自然丢弃（等价原先的降速语义）。

        ReceivePipeline 只在本线程访问；结果经 Signal 回 UI 线程。不碰 self._cap。
        gen 为启动时的接收代际：停止超时残留的旧线程在此发现代际不匹配，
        不会把旧帧喂进新 pipe（旧帧 timestamp 会污染新会话的已用/速率统计）。

        容错：单帧异常只跳过，连续失败超阈值才停机。旧实现把 cvtColor 放在 try 外、
        try 里又只接 ValueError，灰度/4 通道帧的 cv2.error、落盘 OSError、解压
        zlib.error 都会让线程静默死亡——而 UI 仍显示"接收中"、预览照刷，
        永远不再出结果。这正是"偶发长时间超时无输出"最直接的来源。
        """
        consecutive_errors = 0
        while not self._decode_stop.is_set():
            if gen != self._recv_generation:
                return   # 本线程属旧代：新会话已启动，静默退出不 emit
            try:
                frame = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if gen != self._recv_generation:
                return   # 取帧等待期间新会话已启动：丢弃该帧，本线程静默退出
            pipe = self._pipe
            if pipe is None:
                continue
            try:
                # 颜色转换/降采样/解码全部纳入单帧容错（cvtColor 也可能对坏帧抛 cv2.error）
                image = _prepare_decode_image(frame, pipe)
                missed_before = getattr(pipe, "missed_images", 0)
                started = time.perf_counter()
                pipe.process_image(image)
                self._decode_ms = (time.perf_counter() - started) * 1000.0
            except SessionCorruptError as e:
                self._decode_error.emit(str(e))   # 会话级损坏：重试无意义，停机报错
                return
            except Exception as e:  # noqa: BLE001 — 单帧异常不得杀死整条线程
                consecutive_errors += 1
                if consecutive_errors >= _DECODE_ERROR_LIMIT:
                    self._decode_error.emit(f"连续 {consecutive_errors} 帧解码失败：{e}")
                    return
                continue
            consecutive_errors = 0
            # 整帧一个码都没扫到 → 连续计数；连续 _FAIL_DUMP_STREAK 帧仍无码就自动留一张
            # 原图样本（每轮接收最多 _FAIL_DUMP_MAX 张）：真机上识别不出的画面极难口述，
            # 存下来才能离线用 zxing/参数对比定位，而不是靠猜。
            if getattr(pipe, "missed_images", 0) > missed_before:
                self._fail_streak += 1
                if (self._fail_streak >= _FAIL_DUMP_STREAK
                        and self._fail_dumped < _FAIL_DUMP_MAX):
                    path = self._save_debug_frame(frame, "fail")
                    if path:
                        self._fail_dumped += 1
                        self._fail_streak = 0
                        self._capture_status.emit(f"已留存识别失败样本：{path}")
            else:
                self._fail_streak = 0
            self._decode_progress.emit()

    def _check_recv_threads(self):
        """接收期间的线程存活看门狗（随 500ms 统计定时器触发）。

        线程异常退出后没有 self._decode_thread = None 的路径，而 _on_decode_progress
        以 _capture_thread is None 判"是否接收中"，所以线程死了 UI 仍显示接收中。
        这里按心跳检查 is_alive()：解码线程异常退出就自动重启（pipe/队列都在 UI 侧，
        重启不影响会话）；短时间内反复死亡或超过总次数则停机报错。
        采集线程的容错在 _capture_loop 内部重试，这里只兜"线程彻底没了"的情况。
        """
        if not self._r_start.isChecked():
            return
        dthread = self._decode_thread
        if (dthread is not None and not dthread.is_alive()
                and not self._decode_stop.is_set()
                and self._pipe is not None and self._decode_queue is not None):
            now = time.monotonic()
            if (self._decode_restarts >= _MAX_DECODE_RESTARTS
                    or now - self._last_decode_restart < _THREAD_RESTART_COOLDOWN):
                self._decode_error.emit("解码线程反复异常退出，接收已停止")
                return
            self._decode_restarts += 1
            self._last_decode_restart = now
            self.statusBar().showMessage(
                f"解码线程异常退出，已自动重启（第 {self._decode_restarts} 次）")
            self._decode_thread = threading.Thread(
                target=self._decode_loop, args=(self._decode_queue, self._recv_generation),
                name="recv-decode", daemon=True)
            self._decode_thread.start()
            return
        cthread = self._capture_thread
        if (cthread is not None and not cthread.is_alive()
                and not self._capture_stop.is_set()):
            # 采集线程是打开设备的持有者，异常退出后重开容易与驱动状态打架；
            # _capture_loop 已内部重试过一次，这里再见到死亡就明确停机。
            self._decode_error.emit("采集线程异常退出，接收已停止")

    def _on_decode_progress(self):
        """UI 线程：后台解码完一帧后的进度刷新（接收已停止时直接忽略）。

        进度条与左下角状态栏都直接取 pipe.progress（会话已恢复源块占比，唯一权威
        来源），不再做平滑动画——动画会让进度条滞后于真实值，两处显示不一致时
        用户无从判断哪个是准的。round 与状态栏 `:.0%` 的取整口径保持一致。
        """
        pipe = self._pipe
        if self._capture_thread is None or pipe is None:
            return
        self._r_progress.setValue(round(pipe.progress * 100))
        self._update_r_count()
        self._update_r_missing()
        self._update_r_stats()
        self._refresh_cam_meta()   # 采集实况常驻在预览标题旁（状态栏会被本函数的进度覆盖）
        self.statusBar().showMessage(f"进度 {pipe.progress:.0%}")
        if pipe.result is not None:
            self._on_received()
            self._stop_recv()

    def _on_decode_error(self, message: str):
        """UI 线程：后台解码/采集报错（接收已停止时忽略，避免重复弹窗）。"""
        if self._capture_thread is None:
            return
        self._stop_recv()
        QMessageBox.warning(self, "错误", message)

    def _capture_loop(self, candidates: list[tuple[int, str]], gen: int):
        """采集线程入口：带崩溃兜底。

        旧实现让采集线程直接执行 read 循环，任何未捕获异常（cv2.error / 驱动异常）
        都会让线程无声退出；而 UI 以线程对象非 None 判断"接收中"，界面会永远停在
        接收中且不再出任何结果。这里兜住所有异常：释放可能仍被持有的设备、
        重试一次（驱动协商偶发错误），仍失败则明确报错停机。
        """
        for attempt in range(2):
            try:
                self._capture_loop_once(candidates, gen)
                return
            except Exception as e:  # noqa: BLE001 — 采集线程不得静默死亡
                cap = getattr(self, "_cap", None)
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:  # noqa: BLE001, S110 — 兜底释放尽力而为
                        pass
                    self._cap = None
                if gen != self._recv_generation or self._capture_stop.is_set():
                    return   # 本线程属旧代或已请求停止：静默退出
                if attempt >= 1:
                    self._decode_error.emit(f"采集线程异常：{e}")
                    return
                self._capture_status.emit(f"采集异常（{e}），正在重试…")
                time.sleep(0.2)

    def _capture_loop_once(self, candidates: list[tuple[int, str]], gen: int):
        """采集线程本轮：按候选设备顺序 open + 实测画面 + 循环 read，黑屏自动顺延。

        open/read/release 全在此线程，UI 经 _frame_ready 收帧刷新预览、
        经 _decode_queue 喂解码线程。失败经 _decode_error 报。
        gen 为启动时的接收代际：停止超时残留的旧线程据此识别自己已过期，
        不再向新会话投帧/报错（open 慢的旧线程弹错误框会打断新一轮接收）。
        candidates 为 (设备索引, 探测后端) 列表：探测时记下的后端优先复用
        （热插拔设备常只有 DSHOW 可见），打开失败再回退 auto 全面重试。

        候选顺延（两道防线）：
        1. 打开阶段：_open_usable_camera 实测「read 出非黑帧」才算可用——虚拟
           摄像头空载的黑帧（含带噪点的黑帧）、isOpened 成功但 read 失败的假可用
           设备都在这里被甄别掉；探测帧间留短间隔，容忍传感器/协商热启动。
        2. 打开后宽限期：_run_capture_reads 在打开后前几秒监控「连续纯黑帧」——
           打开时正常、设链路参数（MJPG/720p/60fps）后变黑的设备也会被兜住，
           自动顺延下一候选并同步下拉框；全部候选都不行才报错。
        """
        remaining = list(candidates)
        while (remaining and gen == self._recv_generation
               and not self._capture_stop.is_set()):
            idx, backend = remaining.pop(0)
            cap = self._open_usable_camera(idx, backend, gen)
            if cap is None:
                if gen == self._recv_generation and not self._capture_stop.is_set():
                    self._capture_status.emit(f"设备 {idx} 无画面，尝试下一个设备…")
                continue
            # 链路调优（手机虚拟摄像头尤其关键）：
            # - MJPG 压缩：USB 虚拟摄像头默认 YUYV 未压缩，高分辨率带宽不足会严重掉帧 → 进度慢。
            # - 缓冲 1 帧：默认多帧缓冲会让 read() 返回过时画面，屏幕 QR 快速切换时识别率暴跌。
            # - 固定 720p：在细节清晰度与带宽之间取平衡。set() 失败（设备不支持）静默忽略。
            self._apply_capture_settings(cap, backend)
            self._report_capture_format(cap, idx)
            if gen != self._recv_generation:
                cap.release()   # open 期间新一轮已启动：本线程属旧代，静默释放退出
                return
            # 标记 open 成功，供 _on_decode_progress / _on_decode_error 判"接收中"
            self._cap = cap
            # 顺延成功：下拉框同步到实际设备。必须放在 _cap 赋值之后——监听方
            # （含测试与 UI）收到该信号即认为设备已投入使用，早发会读到 None。
            if idx != candidates[0][0] and gen == self._recv_generation:
                self._device_in_use.emit(idx)
            outcome = self._run_capture_reads(cap, idx, gen, backend)
            if outcome == "expo_revert":
                # 短曝光无改善：重开设备以恢复厂商默认（自动）曝光，再继续接收。
                # 会话在 UI 侧，重开不丢进度。
                if gen == self._recv_generation and not self._capture_stop.is_set():
                    cap.release()
                    self._cap = None
                    time.sleep(0.2)
                    continue
                return
            if outcome == "readfail":
                # 假可用设备：isOpened 成功但 read 持续失败（MSMF 协商错误），
                # 不静默黑屏卡死，明确报错提示换设备/检查驱动。
                if gen == self._recv_generation:
                    self._decode_error.emit(
                        f"摄像头 {idx} 无法读取画面，设备可能未真正出帧，请改选其它设备或检查驱动")
                return
            if outcome == "slow":
                # 原地重开也救不回的低帧率：有候选则顺延，否则明确报错（不要笼统说“无画面”）
                if gen == self._recv_generation and not self._capture_stop.is_set():
                    if remaining:
                        self._capture_status.emit(
                            f"设备 {idx} 帧率持续过低，尝试下一个设备…")
                    else:
                        self._decode_error.emit(
                            f"摄像头 {idx} 帧率持续过低（低于 {_MIN_FPS:.0f} fps），"
                            "请重新插入设备或改选其它设备")
                        return
                continue
            if outcome == "blackout":
                # 打开后宽限期内持续纯黑：有候选则顺延，无候选走下方全部失败报错
                if (gen == self._recv_generation and not self._capture_stop.is_set()
                        and remaining):
                    self._capture_status.emit(f"设备 {idx} 画面无内容，尝试下一个设备…")
                continue
            return   # outcome == "stop"：正常停止（capture_stop 置位 / 代际过期）
        if gen == self._recv_generation and not self._capture_stop.is_set():
            self._decode_error.emit(
                "所有摄像头均无法出画面，请在设备下拉框中选择其它设备")

    def _open_usable_camera(self, idx: int, backend: str, gen: int):
        """打开 idx 并实测「read 出非黑帧」才算可用；失败返回 None（cap 已释放）。

        虚拟摄像头空载时 read 返回"成功"但全是黑帧（黑屏无画面的根因），isOpened
        成功但 read 失败也常见（MSMF 重新协商）。打开后连读若干帧（帧间留短间隔，
        容忍传感器/协商热启动——头几帧黑属正常），任一帧非黑即视为有画面；
        全黑/读不出帧按 _OPEN_ATTEMPTS 重试，仍失败返回 None，由 _capture_loop
        顺延到下一个候选设备。

        backend 为探测时记录的成功后端（热插拔设备常只有 DSHOW 可见）：优先用
        同一后端打开；若该后端已失效（设备插拔后索引漂移/驱动重协商），再回退
        auto 全面重试一遍。
        """
        for bk in ((backend, "auto") if backend != "auto" else ("auto",)):
            for _ in range(_OPEN_ATTEMPTS):
                if gen != self._recv_generation or self._capture_stop.is_set():
                    return None
                c = open_camera(idx, backend=bk)
                if not c.isOpened():
                    c.release()
                    time.sleep(0.3)
                    continue
                slow_reads = 0
                for _ in range(_OPEN_PROBE_FRAMES):
                    started = time.monotonic()
                    try:
                        ok, frame = c.read()
                    except Exception:  # noqa: BLE001 — 个别设备 read 抛异常按失败处理
                        ok, frame = False, None
                    # 驱动落入 1fps 状态时 read 精确阻塞约 1s：8 帧探测会白等 8 秒，
                    # 连续慢读直接放弃本轮（交给重试/下一候选），避免"打开就很慢"。
                    if time.monotonic() - started >= _SLOW_READ_SECONDS:
                        slow_reads += 1
                        if slow_reads >= _SLOW_READ_LIMIT:
                            break
                    elif ok and frame is not None and not _is_black_frame(frame):
                        return c
                    time.sleep(_OPEN_PROBE_INTERVAL)
                c.release()
                time.sleep(0.3)
        return None

    def _apply_capture_settings(self, cap, backend: str = "auto") -> None:
        """链路调优参数（设备不支持时 set 返回 False，个别驱动直接抛异常——均忽略）。"""
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            # DSHOW 不支持 BUFFERSIZE（实测返回 False），每次 set 还带约 400ms 协商开销
            if backend != "dshow":
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self._negotiate_capture_mode(cap)
        except Exception:  # noqa: BLE001, S110 — 个别驱动 set/get 抛异常；不支持按默认参数继续
            pass

    def _downgrade_capture_mode(self) -> bool:
        """把采集档位降一档（如 1080p→720p）；已在最低档或档位未启用时返回 False。

        用途：1080p 在 USB 2.0 口上可能只跑到 3~5fps。低帧率重开若仍请求同一档，
        等于白开一次；先降档再重开，用 1.5 倍的模块像素换回可用帧率。
        """
        if self._capture_mode_idx < len(_CAPTURE_MODES) - 1:
            self._capture_mode_idx += 1
            return True
        return False

    def _negotiate_capture_mode(self, cap) -> tuple[int, int, int]:
        """按 _CAPTURE_MODES 逐档协商「宽/高/帧率」，回读校验后返回实际采用的一档。

        为什么必须回读：set() 不返回是否生效，而 USB 摄像头常见两种静默降级——
        (1) 请求 1080p 被协商成 720p（低带宽口/驱动不支持该组合）；
        (2) 请求 30fps 被协商成个位数且不自愈。
        宽度回读为 0 表示该后端不报告尺寸，此时按「已接受」处理，交给帧率兜底。
        增益来源：每模块相机像素从 ~3.7 提到 ~5.5，是把 1200B/2200B 帧长从「扫不出」
        拉回「扫得出」的关键（详见 _CAPTURE_MODES 注释）。
        """
        chosen = _CAPTURE_MODES[-1]
        for w, h, fps in _CAPTURE_MODES[self._capture_mode_idx:]:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, fps)
            aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            afps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            size_ok = aw <= 0 or ah <= 0 or (aw >= w * 0.9 and ah >= h * 0.9)
            # 高帧率档（60fps）必须真生效才有意义：60fps 的价值就是强制曝光 ≤16.7ms，
            # 若驱动报 30 就等于没换档，此时继续往下试（下一档正是 720p30，无损失）。
            fps_ok = fps < 50 or afps <= 0 or afps >= 45
            if size_ok and fps_ok:
                chosen = (w, h, fps)
                break
        # 帧率兜底：部分机型把 30 协商成个位数且不自愈（实测冷开 4/15），明确回落 30。
        applied = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if applied and applied < _CAPTURE_FPS_FALLBACK:
            cap.set(cv2.CAP_PROP_FPS, _CAPTURE_FPS_FALLBACK)
            chosen = (chosen[0], chosen[1], _CAPTURE_FPS_FALLBACK)
        return chosen

    def _report_capture_format(self, cap, idx: int) -> None:
        """回读协商结果并记下，供「实际出帧是否与协商一致」校验。

        set() 一律不检查返回值，回读才能暴露「请求 1080p 实际 720p / 请求 30fps
        实际个位数」这类静默降级（状态栏给用户看一次）。
        """
        try:
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        except Exception:  # noqa: BLE001 — 回读失败不影响采集
            return
        if w <= 0 or h <= 0:
            return
        self._capture_negotiated = (w, h)
        code = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")
        fps_txt = f" @ {fps:.0f}fps" if fps > 0 else ""
        self._cam_format = f"{w}×{h}{fps_txt} {code}".strip()
        self._refresh_cam_meta()
        self._capture_status.emit(f"设备 {idx}: {self._cam_format}")

    def _check_actual_frame_size(self, cap, idx: int, frame) -> None:
        """首帧到达时核对真实出帧尺寸：部分驱动报 1920 却只出 640×480。

        这类「报大出小」是拍屏识别率骤降的隐形原因——协商看起来是 1080p，
        实际每模块像素还是 720p 的三分之一。不符时在预览标题旁常驻标红。
        """
        fh, fw = frame.shape[:2]
        self._cam_actual = (fw, fh)
        neg = self._capture_negotiated
        if neg and (fw, fh) != neg:
            self._capture_status.emit(
                f"设备 {idx} 实际出帧 {fw}×{fh}（协商 {neg[0]}×{neg[1]} 未生效），"
                "识别率会受影响")
        self._refresh_cam_meta()

    def _refresh_cam_meta(self) -> None:
        """刷新预览标题旁的采集实况（常驻显示，不会被进度消息冲掉）。"""
        if not hasattr(self, "_cam_meta"):
            return
        parts = []
        if self._cam_format:
            parts.append(self._cam_format)
        neg, act = self._capture_negotiated, self._cam_actual
        if act and neg and act != neg:
            parts.append(f"⚠实际出帧 {act[0]}×{act[1]}")
        elif act:
            parts.append(f"出帧 {act[0]}×{act[1]}")
        if self._decode_ms:
            parts.append(f"解码 {self._decode_ms:.1f}ms")
        measured = self._module_pixels() if self._r_start.isChecked() else None
        if measured is not None:
            px, modules = measured
            flag = "" if px >= _GOOD_MODULE_PX else "（偏小，请靠近）"
            parts.append(f"每模块 {px:.1f}px/{modules}模块{flag}")
        text = " · ".join(parts)
        self._cam_meta.setText(text)
        # 协商与实际不一致属异常，标红提醒用户换口/换线
        self._cam_meta.setStyleSheet(
            "color:#DC2626; font-size:11px; font-weight:600;" if (act and neg and act != neg)
            else "color:#64748B; font-size:11px;")

    def _debug_dir(self) -> str:
        d = os.path.join(os.path.expanduser("~"), ".qrferry", "debug")
        os.makedirs(d, exist_ok=True)
        return d

    def _save_debug_frame(self, frame, tag: str) -> str | None:
        """把相机原始帧存成 PNG（失败样本/手动导出共用）；返回路径或 None。"""
        try:
            name = time.strftime(f"{tag}-%Y%m%d-%H%M%S.png")
            path = os.path.join(self._debug_dir(), name)
            if not cv2.imwrite(path, _as_bgr(frame)):
                return None
            return path
        except Exception:  # noqa: BLE001 — 诊断落盘失败绝不能影响接收
            return None

    def _dump_frame(self) -> None:
        """「保存画面」按钮：把当前帧存盘并提示路径（排查识别问题用）。"""
        frame = self._last_raw_frame
        if frame is None:
            self.statusBar().showMessage("还没有画面可保存（请先开始接收）")
            return
        path = self._save_debug_frame(frame, "manual")
        self.statusBar().showMessage(
            f"画面已保存：{path}" if path else "保存失败（请检查磁盘权限）")

    def _run_capture_reads(self, cap, idx: int, gen: int, backend: str = "auto") -> str:
        """主采集循环：持续 read 并投帧；退出时释放 cap 并清除 _cap 活跃标记。

        返回退出原因：
        - "stop"：正常停止（capture_stop 置位或代际过期）
        - "readfail"：连续 read 失败超阈值（假可用设备，MSMF 协商错误）
        - "blackout"：打开后宽限期内持续纯黑帧（虚拟摄像头空载/切流后无画面）
        - "slow"：实测帧率持续低于 _MIN_FPS，且原地重开 _REOPEN_ATTEMPTS 次仍无改善
        宽限期内只监控「连续纯黑」；宽限期过后不再监控黑帧——避免传输中暗场景
        （手机息屏、镜头被挡等）被误判为设备故障；read 失败监控始终生效。
        帧率监控全程生效：1fps 状态下 read 返回 ok=True，只靠 read 失败/黑帧都发现不了。
        """
        read_fail = 0
        grace_end = time.monotonic() + _BLACK_GRACE_SECONDS
        black_since: float | None = None
        stamps: deque[float] = deque(maxlen=_FPS_SAMPLE_SIZE)
        warmup_end = time.monotonic() + _FPS_WARMUP_SECONDS
        low_fps_since: float | None = None
        low_fps_warned = False
        reopens_left = _REOPEN_ATTEMPTS
        size_checked = False   # 首帧只核对一次真实出帧尺寸
        self._reset_expo_window()   # 曝光 A/B 观察窗口从本轮重新计时
        outcome = "stop"
        try:
            while not self._capture_stop.is_set():
                try:
                    ok, frame = cap.read()
                except Exception:  # noqa: BLE001 — 个别设备 read 抛异常按失败处理
                    ok, frame = False, None
                if not ok:
                    # 虚拟设备可能 isOpened 成功但 read 持续失败（MSMF 协商错误）。
                    # 无限 continue 会静默黑屏卡死；累计失败超阈值即判定假可用设备。
                    read_fail += 1
                    if read_fail >= _READ_FAIL_LIMIT:
                        outcome = "readfail"
                        break
                    continue
                read_fail = 0
                # 旧代线程：release 后静默退出，不向新会话投帧/刷预览
                if gen != self._recv_generation:
                    outcome = "stop"
                    break
                # 首帧核对真实出帧尺寸（「报大出小」会让识别率静默变差）
                if size_checked is False:
                    size_checked = True
                    self._check_actual_frame_size(cap, idx, frame)
                now = time.monotonic()
                stamps.append(now)
                if now < grace_end and _is_black_frame(frame):
                    if black_since is None:
                        black_since = now
                    elif now - black_since >= _BLACK_CONTINUOUS_SECONDS:
                        outcome = "blackout"
                        break
                else:
                    black_since = None
                # 帧率看门狗：1fps 时 read 仍是 ok=True，只有实测间隔能发现。
                if now >= warmup_end and len(stamps) >= _FPS_MIN_SAMPLES:
                    span = stamps[-1] - stamps[0]
                    fps = (len(stamps) - 1) / span if span > 0 else 0.0
                    if fps < _MIN_FPS:
                        if low_fps_since is None:
                            low_fps_since = now
                        elif now - low_fps_since >= _LOW_FPS_SECONDS:
                            if reopens_left <= 0:
                                # 重开无效：极低帧率（1fps 状态）停机顺延；2~5fps 只提示一次，
                                # 继续以低速接收——慢总比直接中断好。
                                if fps < _CRITICAL_FPS:
                                    outcome = "slow"
                                    break
                                if not low_fps_warned:
                                    low_fps_warned = True
                                    self._capture_status.emit(
                                        f"设备 {idx} 帧率偏低（{fps:.1f} fps），继续接收")
                                low_fps_since = None
                                # 不 continue：这一帧照常投递，只是不再重复提示
                            else:
                                reopens_left -= 1
                                # 分辨率优先有代价：USB 2.0 口/弱驱动能"报"1080p 却只出
                                # 3~5fps。此时先把采集档位降一档（1080p→720p）再重开，
                                # 而不是原样重开同档——否则重开多少次都还是低帧率。
                                downgraded = self._downgrade_capture_mode()
                                self._capture_status.emit(
                                    f"设备 {idx} 帧率过低（{fps:.1f} fps），"
                                    + ("降低采集分辨率后重开…" if downgraded else "正在重开…"))
                                cap.release()
                                cap = self._reopen_camera(idx, backend, gen)
                                if cap is None:
                                    # 重开失败可能是"已请求停止/代际过期"，别误报成帧率问题
                                    outcome = ("stop" if (gen != self._recv_generation
                                                          or self._capture_stop.is_set())
                                               else "slow")
                                    break
                                if gen != self._recv_generation or self._capture_stop.is_set():
                                    cap.release()
                                    outcome = "stop"
                                    break
                                self._cap = cap
                                stamps.clear()
                                low_fps_since = None
                                low_fps_warned = False
                                warmup_end = time.monotonic() + _FPS_WARMUP_SECONDS
                                grace_end = time.monotonic() + _BLACK_GRACE_SECONDS
                                continue
                    else:
                        low_fps_since = None
                self._frame_ready.emit(frame)
                # 曝光自寻优：拍屏丢帧的主因不是模糊，而是「曝光窗口横跨屏幕换码瞬间」
                # 产生的撕裂帧（上半旧码/下半新码），像素再多也解不出。缩短曝光能大幅
                # 降低此概率。这里不猜——实测 4 秒识别率做 A/B，只有确实变好才保留，
                # 否则重开设备恢复自动曝光。
                expo_outcome = self._expo_step(cap, frame)
                if expo_outcome == "revert":
                    outcome = "expo_revert"
                    break
                # 投递到"只保留最新一帧"的槽位：槽满说明解码慢，丢旧帧投新帧
                q = self._decode_queue
                if q is not None:
                    if q.full():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        q.put_nowait(frame)
                    except queue.Full:
                        pass
        finally:
            if cap is not None:   # 重开失败时 cap 已被置 None（旧句柄已在重开前释放）
                cap.release()
            if gen == self._recv_generation:
                self._cap = None   # 仅当前代线程允许清除活跃标记（旧代线程不得误清新会话的 cap）
        return outcome

    def _expo_step(self, cap, frame) -> str | None:
        """曝光 A/B 自寻优状态机：基线 → 试短曝光 → 保留或回退（返回 "revert"）。

        只统计「本帧是否解出了码」——这正是撕裂帧的直接表征，与取景大小无关，
        所以不受用户距离变化干扰。样本不足或驱动不支持时安静跳过，绝不打扰用户。
        """
        if self._expo_state == "off":
            return None
        pipe = self._pipe
        if pipe is None:
            return None
        missed = getattr(pipe, "missed_images", 0)
        # 用 missed_images 的增量判断本帧是否解出（解码在工作线程，稍滞后也够用）
        clean = missed == self._expo_last_missed
        self._expo_last_missed = missed
        self._expo_frames += 1
        if clean:
            self._expo_clean += 1
        now = time.monotonic()
        if now - self._expo_t0 < _EXPO_WINDOW_SECONDS or self._expo_frames < _EXPO_MIN_FRAMES:
            return None
        rate = self._expo_clean / max(1, self._expo_frames)
        if self._expo_state == "baseline":
            if rate >= 0.95:
                self._expo_state = "off"      # 几乎不丢帧，没有优化空间
                return None
            if not self._try_short_exposure(cap):
                self._expo_state = "off"      # 驱动不支持手动曝光
                return None
            self._expo_base_rate = rate
            self._expo_state = "trying"
            self._reset_expo_window()
            self._capture_status.emit(f"尝试缩短曝光以降低撕裂帧（当前识别率 {rate:.0%}）…")
            return None
        # trying：与基线比较，提升不足 8% 视为无效
        if rate > self._expo_base_rate * 1.08:
            self._expo_state = "off"
            self._capture_status.emit(
                f"已启用短曝光：识别率 {self._expo_base_rate:.0%} → {rate:.0%}")
            return None
        self._expo_state = "off"
        self._capture_status.emit(
            f"短曝光无改善（{self._expo_base_rate:.0%} → {rate:.0%}），恢复自动曝光")
        return "revert"

    def _try_short_exposure(self, cap) -> bool:
        """把手动曝光调到约 1/100s；失败（驱动不支持）返回 False。

        DSHOW 用 0.25 表示手动、-log2(秒) 表示曝光值；MSMF 用 0/1。两套都试一遍，
        以回读是否真的变小为准——很多内置摄像头只支持自动曝光，此时直接放弃。
        """
        try:
            before = float(cap.get(cv2.CAP_PROP_EXPOSURE) or 0)
            for auto_val, manual_val in ((0.25, -7.0), (0.0, -7.0)):
                try:
                    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, auto_val)
                    ok = cap.set(cv2.CAP_PROP_EXPOSURE, manual_val)
                except Exception:  # noqa: BLE001, S110 — 个别驱动直接抛，换下一套参数
                    pass
                else:
                    after = float(cap.get(cv2.CAP_PROP_EXPOSURE) or 0)
                    if ok and after != before and after != 0:
                        return True
        except Exception:  # noqa: BLE001 — 不支持就当没有这项能力
            return False
        return False

    def _reset_expo_window(self) -> None:
        self._expo_t0 = time.monotonic()
        self._expo_frames = 0
        self._expo_clean = 0
        self._expo_last_missed = getattr(self._pipe, "missed_images", 0)

    def _reopen_camera(self, idx: int, backend: str, gen: int):
        """原地重开设备并重刷链路参数；失败返回 None。"""
        cap = self._open_usable_camera(idx, backend, gen)
        if cap is None:
            return None
        self._apply_capture_settings(cap, backend)
        return cap

    def _on_frame_ready(self, frame):
        """UI 线程：采集线程送来的最新帧，刷新预览（接收已停止时直接忽略）。"""
        if self._capture_thread is None or self._pipe is None:
            return
        self._last_raw_frame = frame   # 「保存画面」/失败样本留存取这一帧
        pix = QPixmap.fromImage(_qimage_from_cv(frame))
        self._cam_label.setPixmap(pix.scaled(self._cam_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _on_received(self):
        r = self._pipe.result
        comp_txt = _compression_desc(r)
        if r.content_type == ContentType.TEXT:
            self._r_result.setPlainText(r.data.decode("utf-8", errors="replace"))
            self._r_copy.show()
            self._r_save.hide()
            self.statusBar().showMessage(f"文本接收完成（{comp_txt}）")
        else:
            filename = safe_filename(r.filename)
            self._r_result.setPlainText(f"文件接收完成：{filename}\n大小：{len(r.data)} 字节\n{comp_txt}")
            self._r_copy.hide()
            self._r_save.show()
            self.statusBar().showMessage(f"文件接收完成，等待保存：{filename}")
        self._update_r_count()
        self._update_r_stats()

    def _save_received_file(self):
        if self._pipe is None or self._pipe.result is None:
            return
        r = self._pipe.result
        if r.content_type != ContentType.FILE:
            return

        filename = safe_filename(r.filename)
        default_dir = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
        if not default_dir or not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")
        default_path = os.path.join(default_dir, filename)
        extension = os.path.splitext(filename)[1]
        file_filter = (f"{extension[1:].upper()} 文件 (*{extension});;所有文件 (*)"
                       if extension else "所有文件 (*)")
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "保存接收文件", default_path, file_filter)
        if not path:
            return
        if extension and not os.path.splitext(path)[1] and selected_filter != "所有文件 (*)":
            path += extension
        try:
            with open(path, "wb") as f:
                f.write(r.data)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self._r_result.setPlainText(f"文件已保存：{path}")
        self.statusBar().showMessage(f"文件已保存：{path}")

    def _copy_result(self):
        from PySide6.QtWidgets import QApplication
        text = self._r_result.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage("已复制到剪贴板")
