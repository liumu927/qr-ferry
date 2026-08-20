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
from qrferry.app.receive_pipeline import ReceivePipeline, safe_filename
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

# 采集请求宽度，也是解码降采样的回退目标：单一真相源。
# 1280×720 已被验证为「解码足够快、QR 模块像素充足」的健康工作点（修复前一直在此尺寸直解）。
# 仅当设备无视请求、实际输出宽度超过此值（如高拍仪强出 1080p=1920 宽）时，才降回此宽度——
# 1080p 全图扫描成本 ~O(像素数)，降回 1280 提速 ~2.25x，且像素仍够 zxing 解码。
# 阈值不可低于采集请求宽度，否则会误伤所有遵循请求的摄像头（每帧无谓 resize + 模块缩水）。
_CAPTURE_WIDTH = 1280

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
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < 15.0 and float(gray.max()) < 40.0

class MainWindow(QMainWindow):
    GRID_OPTIONS = (("1×1", (1, 1)), ("2×2", (2, 2)), ("3×3", (3, 3)))
    CODEC_OPTIONS = (("标准QR（推荐）", "qr"), ("彩色码（实验）", "color"))
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
        # 全屏遮罩窗口（发送「全屏展示」勾选时使用）：须在 _build_ui 之前初始化，
        # eventFilter 在构建窗口控制按钮时即可能被触发。
        self._fullscreen_overlay: QWidget | None = None
        self._fs_label: QLabel | None = None
        self._fs_pix: QPixmap | None = None
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
            f"background:{qr_bg}; border:none; border-radius:16px; color:{sub_txt};")
        self._cam_label.setStyleSheet(
            f"background:{cam_bg}; border:1px solid {border}; border-radius:20px; color:{sub_txt};")
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

        left.addWidget(section("参数"))
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
        # 全屏展示：勾选后发送期间 QR 显示在独立全屏遮罩窗口中（更大、更远可扫）
        self._s_fullscreen = QCheckBox("全屏展示（遮罩层）")
        self._s_fullscreen.setChecked(True)   # 默认开启：首屏即获得最大扫码空间
        self._s_fullscreen.setToolTip("发送时在全屏黑色遮罩中显示二维码，码体更大，手机可离更远扫描")
        left.addWidget(self._s_fullscreen)
        left.addStretch(1)
        lay.addWidget(left_w)

        # ── 右：发送预览卡（标题 + 状态徽标 + QR 区）──
        right_w = QFrame()
        right_w.setObjectName("card")
        right = QVBoxLayout(right_w)
        right.setContentsMargins(12, 12, 12, 12)
        right.setSpacing(10)
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
        self._qr_label.setMinimumSize(480, 480)
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
        ctrl_row.addWidget(QLabel("码制"))
        self._r_codec = QComboBox(); self._r_codec.addItems([n for n, _ in self.CODEC_OPTIONS])
        self._r_codec.setFixedWidth(158)
        ctrl_row.addWidget(self._r_codec)
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
        phead.addStretch(1)
        self._r_badge = QLabel("等待接收")
        self._r_badge.setObjectName("badge")
        phead.addWidget(self._r_badge)
        pv.addLayout(phead)
        self._cam_label = QLabel("（摄像头预览）")
        self._cam_label.setAlignment(Qt.AlignCenter)
        self._cam_label.setMinimumSize(480, 180)
        pv.addWidget(self._cam_label, 1)
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
        # 全屏遮罩：ESC 键退出；点击遮罩/QR 任意处退出；resize 时重铺 QR
        if obj is self._fullscreen_overlay or obj is self._fs_label:
            if t == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self._close_fullscreen_overlay()
                return True
            if t == QEvent.MouseButtonPress:
                self._close_fullscreen_overlay()
                return True
            if t == QEvent.Resize or t == QEvent.Show:
                self._refresh_fullscreen_pix()
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
            self._file_path = path
            name = os.path.basename(path)
            self._s_filelabel.setFullText(name)
            self._s_filelabel.setToolTip(path)
            self._update_optimize_preview()

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
            self._show_fullscreen_overlay()
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
        self._show_fullscreen_overlay()

    def _stop_send(self):
        self._send_timer.stop()
        if self._s_start.isChecked():
            self._s_start.setChecked(False)
        self._s_start.setText("开始发送")
        self._update_running_icons()
        self._s_badge.setText("待发送")
        self._send_start_ts = None   # 重置速率统计，下次发送重新计时
        self._close_fullscreen_overlay()

    # ────────────── 全屏遮罩（发送「全屏展示」勾选时）──────────────
    def _ensure_fullscreen_overlay(self) -> None:
        """懒创建全屏遮罩窗口：无边框、纯黑底、居中大 QR，ESC/点击退出。"""
        if self._fullscreen_overlay is not None:
            return
        ov = QWidget(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        ov.setStyleSheet("background:#000000;")
        ov.setAttribute(Qt.WA_DeleteOnClose, False)
        fs = QVBoxLayout(ov)
        fs.setContentsMargins(24, 24, 24, 24)
        self._fs_label = QLabel(ov)
        self._fs_label.setAlignment(Qt.AlignCenter)
        self._fs_label.setStyleSheet("background:transparent;")
        fs.addWidget(self._fs_label)
        hint = QLabel("点击任意处或按 ESC 退出全屏", ov)
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color:#94A3B8; font-size:13px; background:transparent;")
        fs.addWidget(hint)
        self._fullscreen_overlay = ov
        ov.installEventFilter(self)          # 复用 eventFilter：点击/ESC 退出
        self._fs_label.installEventFilter(self)   # label 覆盖大部分区域，点击也走同一出口

    def _show_fullscreen_overlay(self) -> None:
        """发送启动且勾选「全屏展示」时：显示全屏遮罩，QR 立即铺满。"""
        if not getattr(self, "_s_fullscreen", None) or not self._s_fullscreen.isChecked():
            return
        self._ensure_fullscreen_overlay()
        self._fullscreen_overlay.showFullScreen()
        self._fullscreen_overlay.setFocus(Qt.OtherFocusReason)   # 让 ESC 键直达遮罩
        self._fs_label.raise_()
        self._refresh_fullscreen_pix()

    def _refresh_fullscreen_pix(self) -> None:
        """把当前发送预览的 QR 图按遮罩窗口大小等比放大刷新（保持最近邻锐利）。"""
        if self._fullscreen_overlay is None or not self._fullscreen_overlay.isVisible():
            return
        pix = self._fs_pix
        if pix is None or pix.isNull():
            return
        size = self._fs_label.size()
        if size.width() <= 0 or size.height() <= 0:
            return
        self._fs_label.setPixmap(pix.scaled(size, Qt.KeepAspectRatio, Qt.FastTransformation))

    def _close_fullscreen_overlay(self) -> None:
        """停止发送/退出全屏：隐藏遮罩（保留对象，下次复用）。"""
        if self._fullscreen_overlay is not None and self._fullscreen_overlay.isVisible():
            self._fullscreen_overlay.hide()

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
        # 全屏遮罩同步：缓存原始像素图，遮罩可见时按窗口大小刷新
        self._fs_pix = pix
        self._refresh_fullscreen_pix()
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
        # 全程没出过帧（黑屏/打开失败等）时恢复占位文字，避免残留"正在打开摄像头…"
        if self._cam_label.pixmap() is None:
            self._cam_label.setText("（摄像头预览）")

    def _decode_loop(self, q: queue.Queue, gen: int):
        """后台解码线程：只取槽位里的最新帧，慢解码时旧帧自然丢弃（等价原先的降速语义）。

        ReceivePipeline 只在本线程访问；结果经 Signal 回 UI 线程。不碰 self._cap。
        gen 为启动时的接收代际：停止超时残留的旧线程在此发现代际不匹配，
        不会把旧帧喂进新 pipe（旧帧 timestamp 会污染新会话的已用/速率统计）。
        """
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
            # 颜色转换按码制分支（彩色码 RGB / 标准 QR 灰度），与解码同在工作线程
            if isinstance(pipe.backend, ColorMatrixBackend):
                image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            else:
                image = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # 解码前降采样：仅当设备无视采集请求、实际宽度超过 _CAPTURE_WIDTH 时（如高拍仪
            # 强出 1080p），才降回请求宽度。遵循请求的普通摄像头已输出 1280 宽，此处条件不成立，
            # 不做任何 resize——避免无谓缩放拖慢解码 + QR 模块缩水降低识别率。预览不受影响。
            if image.shape[1] > _CAPTURE_WIDTH:
                h = round(image.shape[0] * _CAPTURE_WIDTH / image.shape[1])
                image = cv2.resize(image, (_CAPTURE_WIDTH, h), interpolation=cv2.INTER_AREA)
            try:
                pipe.process_image(image)
            except ValueError as e:
                self._decode_error.emit(str(e))
                return
            self._decode_progress.emit()

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
        """采集线程：按候选设备顺序 open + 实测画面 + 循环 read，黑屏自动顺延。

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
            if idx != candidates[0][0] and gen == self._recv_generation:
                self._device_in_use.emit(idx)   # 顺延成功：下拉框同步到实际设备
            # 链路调优（手机虚拟摄像头尤其关键）：
            # - MJPG 压缩：USB 虚拟摄像头默认 YUYV 未压缩，高分辨率带宽不足会严重掉帧 → 进度慢。
            # - 缓冲 1 帧：默认多帧缓冲会让 read() 返回过时画面，屏幕 QR 快速切换时识别率暴跌。
            # - 固定 720p：在细节清晰度与带宽之间取平衡。set() 失败（设备不支持）静默忽略。
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, _CAPTURE_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            # 60fps（设备支持时）：曝光随之缩短，降低相机曝光横跨屏幕两帧切换的
            # 时间混帧概率——基准仿真显示混帧是高帧率下的主要丢帧来源。
            cap.set(cv2.CAP_PROP_FPS, 60)
            if gen != self._recv_generation:
                cap.release()   # open 期间新一轮已启动：本线程属旧代，静默释放退出
                return
            # 标记 open 成功，供 _on_decode_progress / _on_decode_error 判"接收中"
            self._cap = cap
            outcome = self._run_capture_reads(cap, idx, gen)
            if outcome == "readfail":
                # 假可用设备：isOpened 成功但 read 持续失败（MSMF 协商错误），
                # 不静默黑屏卡死，明确报错提示换设备/检查驱动。
                if gen == self._recv_generation:
                    self._decode_error.emit(
                        f"摄像头 {idx} 无法读取画面，设备可能未真正出帧，请改选其它设备或检查驱动")
                return
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
                for _ in range(_OPEN_PROBE_FRAMES):
                    try:
                        ok, frame = c.read()
                    except Exception:  # noqa: BLE001 — 个别设备 read 抛异常按失败处理
                        ok, frame = False, None
                    if ok and frame is not None and not _is_black_frame(frame):
                        return c
                    time.sleep(_OPEN_PROBE_INTERVAL)
                c.release()
                time.sleep(0.3)
        return None

    def _run_capture_reads(self, cap, idx: int, gen: int) -> str:
        """主采集循环：持续 read 并投帧；退出时释放 cap 并清除 _cap 活跃标记。

        返回退出原因：
        - "stop"：正常停止（capture_stop 置位或代际过期）
        - "readfail"：连续 read 失败超阈值（假可用设备，MSMF 协商错误）
        - "blackout"：打开后宽限期内持续纯黑帧（虚拟摄像头空载/切流后无画面）
        宽限期内只监控「连续纯黑」；宽限期过后不再监控黑帧——避免传输中暗场景
        （手机息屏、镜头被挡等）被误判为设备故障；read 失败监控始终生效。
        """
        read_fail = 0
        grace_end = time.monotonic() + _BLACK_GRACE_SECONDS
        black_since: float | None = None
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
                if time.monotonic() < grace_end:
                    if _is_black_frame(frame):
                        if black_since is None:
                            black_since = time.monotonic()
                        elif time.monotonic() - black_since >= _BLACK_CONTINUOUS_SECONDS:
                            outcome = "blackout"
                            break
                    else:
                        black_since = None
                self._frame_ready.emit(frame)
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
            cap.release()
            if gen == self._recv_generation:
                self._cap = None   # 仅当前代线程允许清除活跃标记（旧代线程不得误清新会话的 cap）
        return outcome

    def _on_frame_ready(self, frame):
        """UI 线程：采集线程送来的最新帧，刷新预览（接收已停止时直接忽略）。"""
        if self._capture_thread is None or self._pipe is None:
            return
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
