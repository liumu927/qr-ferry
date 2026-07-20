"""PySide6 主窗口 —— 发送/接收双模式（现代风 UI，浅色/深色主题）。

发送：文件或文本 → SendController → 按帧率渲染 QR（单码或网格）到屏幕。
接收：OpenCV 摄像头采集 → ReceivePipeline → 预览 + 进度 + 完成后文本/文件。
"""
from __future__ import annotations

import os
import random
import sys

import cv2
import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QPointF, QRect, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QImage, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSpinBox, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget)

from qrferry.app.receive_pipeline import ReceivePipeline
from qrferry.app.send_controller import SendController, SenderConfig
from qrferry.core.frame import ContentType
from qrferry.qr.backend import StandardQrBackend


def _qimage_from_pil(img: Image.Image) -> QImage:
    if img.mode != "L":
        img = img.convert("L")
    w, h = img.size
    return QImage(img.tobytes(), w, h, w, QImage.Format_Grayscale8).copy()


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


def _build_style(dark: bool, up_arrow: str = "", dn_arrow: str = "") -> str:
    """浅色/深色两套 QSS（参照 AirScan-QR：Tailwind slate/gray/blue）。"""
    if dark:
        bg, card, fg, sub = "#0F172A", "#1E293B", "#E2E8F0", "#94A3B8"
        accent, hover, border, track, editbg = "#3B82F6", "#60A5FA", "#334155", "#334155", "#0B1220"
        tabbar_bg = "#0B1220"
    else:
        bg, card, fg, sub = "#F9FAFB", "#FFFFFF", "#0F172A", "#64748B"
        accent, hover, border, track, editbg = "#2563EB", "#1D4ED8", "#E5E7EB", "#E5E7EB", "#FFFFFF"
        tabbar_bg = "#F9FAFB"
    return f"""
QMainWindow {{ background-color: {bg}; }}
QWidget {{ color: {fg}; background: transparent;
    font-family: "Segoe UI","Microsoft YaHei",sans-serif; font-size: 14px; }}
QTabWidget {{ background: {bg}; }}
QStatusBar {{ background: {bg}; border-top: 1px solid {border}; color: {sub}; }}
QStatusBar::item {{ border: none; }}
QTabWidget::pane {{ border: none; background: {bg}; top: 0px; }}
QFrame#topbar {{ background: {tabbar_bg}; border-bottom: 1px solid {border}; }}
QPushButton#toptab {{ background: transparent; color: {sub}; border: none;
    border-radius: 10px; padding: 12px 28px; font-weight: 700; }}
QPushButton#toptab:hover {{ color: {fg}; }}
QPushButton#toptab:checked {{ background: {card}; color: {accent}; }}
QPushButton#themeBtn {{ background: none; border: none;
    border-radius: 10px; padding: 8px 14px; font-weight: 700; }}
QPushButton {{ background: {accent}; color: #FFFFFF; border: none; border-radius: 12px;
    padding: 10px 24px; font-weight: 700; }}
QPushButton:hover {{ background: {hover}; }}
QPushButton:pressed {{ background: {hover}; }}
QPushButton:disabled {{ background: {border}; color: {sub}; }}
QLineEdit, QSpinBox, QComboBox, QTextEdit {{ background: {editbg}; border: 1px solid {border};
    border-radius: 8px; padding: 8px 12px; selection-background-color: {accent}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {{ border: 1px solid {accent}; }}
QSpinBox::up-button, QSpinBox::down-button {{ background: transparent; border: none; width: 20px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {border}; border-radius: 4px; }}
QSpinBox::up-arrow {{ image: url({up_arrow}); width: 10px; height: 6px; }}
QSpinBox::down-arrow {{ image: url({dn_arrow}); width: 10px; height: 6px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::drop-down:hover {{ background: {border}; border-radius: 4px; }}
QComboBox::down-arrow {{ image: url({dn_arrow}); width: 10px; height: 6px; }}
QComboBox QAbstractItemView {{ background: {card}; border: 1px solid {border};
    padding: 4px; outline: none; selection-background-color: {accent}; selection-color: #FFFFFF; }}
QComboBox QAbstractItemView::item {{ padding: 6px 12px; min-height: 20px; }}
QComboBox QAbstractItemView::item:selected {{ background: {accent}; color: #FFFFFF; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {sub}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {border}; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {sub}; }}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; height: 0; width: 0; }}
QProgressBar {{ background: {track}; border: none; border-radius: 8px; height: 14px;
    text-align: center; color: {sub}; font-size: 11px; font-weight: 700; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 8px; }}
QFrame#card {{ background: {card}; border: 1px solid {border}; border-radius: 16px; }}
QFrame#segmented {{ background: {track}; border-radius: 12px; }}
QPushButton#seg {{ background: transparent; color: {sub}; border: none;
    border-radius: 8px; padding: 8px 16px; font-weight: 700; }}
QPushButton#seg:hover {{ color: {fg}; }}
QPushButton#seg:checked {{ background: {card}; color: {accent}; }}
"""


class MainWindow(QMainWindow):
    GRID_OPTIONS = [("1×1", (1, 1)), ("2×2", (2, 2))]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("qr-ferry · 光学二维码摆渡")
        self.setWindowIcon(_make_icon())
        self.resize(980, 680)
        self._backend = StandardQrBackend()
        self._send_ctrl: SendController | None = None
        self._send_iter = None
        self._send_timer = QTimer(self)
        self._send_timer.timeout.connect(self._tick_send)
        self._pipe: ReceivePipeline | None = None
        self._cap = None
        self._recv_timer = QTimer(self)
        self._recv_timer.timeout.connect(self._tick_recv)
        self._file_path = ""
        self._dark = False
        self._build_ui()
        self._apply_theme()

    # ────────────── 主题 ──────────────
    def _apply_theme(self):
        import tempfile
        sub = "#94A3B8" if self._dark else "#64748B"
        d = tempfile.gettempdir()
        up = os.path.join(d, "qrferry_spin_up.png").replace("\\", "/")
        dn = os.path.join(d, "qrferry_spin_dn.png").replace("\\", "/")
        _arrow_png(up, True, sub)
        _arrow_png(dn, False, sub)
        self.setStyleSheet(_build_style(self._dark, up, dn))
        # 区域背景随主题
        editbg = "#0B1220" if self._dark else "#FFFFFF"
        border = "#334155" if self._dark else "#E5E7EB"
        qr_bg = "#1E293B" if self._dark else "#FFFFFF"      # QR 区：浅色白、深色卡片深（QR 图自带白底居中显示）
        cam_bg = "#1E293B" if self._dark else "#E2E8F0"     # 预览区：浅色浅灰、深色深
        self._qr_label.setStyleSheet(f"background:{qr_bg}; border:1px solid {border}; border-radius:16px; color:#94A3B8;")
        self._cam_label.setStyleSheet(f"background:{cam_bg}; border-radius:16px; color:#64748B;")
        # QTextEdit viewport 背景随主题（QSS background 对 viewport 有时不生效）
        for te in (self._s_textedit, self._r_result):
            te.viewport().setStyleSheet(f"background:{editbg};")

    def _set_theme(self, dark: bool):
        self._dark = dark
        self._apply_theme()
        if hasattr(self, "_theme_btn"):
            self._theme_btn.setText("☀️" if dark else "🌙")
            self._theme_btn.setToolTip("切换到浅色" if dark else "切换到深色")

    # ────────────── UI 构建 ──────────────
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶栏：自定义 QFrame（背景 QSS 在所有平台可靠，替代 QTabWidget 自带 tab bar）
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(54)
        tbar = QHBoxLayout(topbar)
        tbar.setContentsMargins(16, 0, 12, 0)
        tbar.setSpacing(4)
        self._tab_send = QPushButton("发送")
        self._tab_send.setObjectName("toptab")
        self._tab_send.setCheckable(True)
        self._tab_send.setChecked(True)
        self._tab_recv = QPushButton("接收")
        self._tab_recv.setObjectName("toptab")
        self._tab_recv.setCheckable(True)
        tgrp = QButtonGroup(topbar)
        tgrp.addButton(self._tab_send)
        tgrp.addButton(self._tab_recv)
        self._tab_send.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        self._tab_recv.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        tbar.addWidget(self._tab_send)
        tbar.addWidget(self._tab_recv)
        tbar.addStretch(1)
        # 主题切换按钮（顶栏最右）
        self._theme_btn = QPushButton("🌙")
        self._theme_btn.setObjectName("themeBtn")
        self._theme_btn.setCursor(Qt.PointingHandCursor)
        self._theme_btn.setToolTip("切换到深色")
        self._theme_btn.clicked.connect(lambda: self._set_theme(not self._dark))
        tbar.addWidget(self._theme_btn)
        root.addWidget(topbar)

        # 内容区：QTabWidget 隐藏自带 tab bar，由顶栏按钮驱动切换
        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.addTab(self._build_send_tab(), "发送")
        self.tabs.addTab(self._build_recv_tab(), "接收")
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _build_send_tab(self) -> QWidget:
        tab = QWidget()
        lay = QHBoxLayout(tab)
        left = QVBoxLayout()

        # iOS 风分段控件：文件 / 文本
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
        sl.addWidget(self._s_file)
        sl.addWidget(self._s_text)
        left.addWidget(seg)

        # 文件状态（左）+ 选择按钮（右），同行；按钮与「清空」等宽（100px）
        file_row = QHBoxLayout()
        self._s_filelabel = ElideLabel("（未选择）", Qt.ElideMiddle)
        self._s_filelabel.setStyleSheet("color:#64748B;")
        self._s_filelabel.setToolTip("（未选择）")
        file_row.addWidget(self._s_filelabel, 1)
        self._s_pick = QPushButton("选择")
        self._s_pick.clicked.connect(self._pick_file)
        self._s_pick.setFixedWidth(100)
        file_row.addWidget(self._s_pick)
        left.addLayout(file_row)

        self._s_textedit = QTextEdit()
        self._s_textedit.setPlaceholderText("输入要发送的文本…")
        self._s_textedit.hide()
        left.addWidget(self._s_textedit)
        # 字符统计角标（框内右下角，不计字符限制）
        self._s_count = QLabel("0 字符", self._s_textedit)
        self._s_count.setStyleSheet("color:#64748B; background:rgba(255,255,255,0.85); padding:1px 6px; border-radius:6px; font-size:11px;")
        self._s_textedit.textChanged.connect(self._update_s_count)
        self._s_textedit.viewport().installEventFilter(self)
        # 清空按钮（文本模式时随文本框显示，右对齐，与选择文件等宽）
        clear_row = QHBoxLayout()
        clear_row.addStretch(1)
        self._s_clear = QPushButton("清空")
        self._s_clear.clicked.connect(self._s_textedit.clear)
        self._s_clear.setFixedWidth(100)
        self._s_clear.hide()
        clear_row.addWidget(self._s_clear)
        left.addLayout(clear_row)

        for label, attr, items, cur in (
            ("帧率(FPS)", "_s_fps", None, 8),
            ("纠错级", "_s_ecc", ["L", "M", "Q", "H"], None),
            ("网格", "_s_grid", [n for n, _ in self.GRID_OPTIONS], None),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            if attr == "_s_fps":
                w = QSpinBox(); w.setRange(1, 30); w.setValue(cur)
            else:
                w = QComboBox(); w.addItems(items)
                if attr == "_s_ecc": w.setCurrentText("Q")
            setattr(self, attr, w)
            row.addWidget(w)
            left.addLayout(row)

        self._s_start = QPushButton("开始发送")
        self._s_start.setCheckable(True)
        self._s_start.toggled.connect(self._toggle_send)
        left.addWidget(self._s_start)
        left.addStretch(1)
        left.setContentsMargins(18, 18, 18, 18)
        left.setSpacing(10)
        left_w = QFrame()
        left_w.setObjectName("card")
        left_w.setLayout(left)
        left_w.setFixedWidth(300)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(18)
        lay.addWidget(left_w)

        self._qr_label = QLabel("（待发送）")
        self._qr_label.setAlignment(Qt.AlignCenter)
        self._qr_label.setMinimumSize(480, 480)
        lay.addWidget(self._qr_label, 1)
        return tab

    def _build_recv_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        top = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("摄像头索引"))
        self._r_cam = QSpinBox(); self._r_cam.setRange(0, 9); self._r_cam.setValue(0)
        left.addWidget(self._r_cam)
        # 目录显示（左，溢出省略）+ 选择按钮（右），同行
        dir_row = QHBoxLayout()
        self._r_dir = ElideLabel("./", Qt.ElideMiddle)
        self._r_dir.setStyleSheet("color:#64748B;")
        self._r_dir.setToolTip("./")
        dir_row.addWidget(self._r_dir, 1)
        self._r_pick = QPushButton("选择")
        self._r_pick.clicked.connect(self._pick_save_dir)
        self._r_pick.setFixedWidth(100)
        dir_row.addWidget(self._r_pick)
        left.addLayout(dir_row)
        row = QHBoxLayout(); row.addWidget(QLabel("采集FPS"))
        self._r_fps = QSpinBox(); self._r_fps.setRange(1, 30); self._r_fps.setValue(10)
        row.addWidget(self._r_fps); left.addLayout(row)
        self._r_start = QPushButton("开始接收")
        self._r_start.setCheckable(True)
        self._r_start.toggled.connect(self._toggle_recv)
        left.addWidget(self._r_start)
        left.addStretch(1)
        left.setContentsMargins(18, 18, 18, 18)
        left.setSpacing(10)
        left_w = QFrame()
        left_w.setObjectName("card")
        left_w.setLayout(left)
        left_w.setFixedWidth(260)
        top.setContentsMargins(18, 18, 18, 18)
        top.setSpacing(18)
        top.addWidget(left_w)
        self._cam_label = QLabel("（摄像头预览）")
        self._cam_label.setAlignment(Qt.AlignCenter)
        self._cam_label.setMinimumSize(480, 360)
        top.addWidget(self._cam_label, 1)
        lay.addLayout(top, 1)
        self._r_progress = QProgressBar(); self._r_progress.setValue(0); lay.addWidget(self._r_progress)
        # 缺块明细：传输中展示仍未恢复的源块索引，缺 0 块或未开始时隐藏
        self._r_missing = QLabel("")
        self._r_missing.setStyleSheet("color:#DC2626; font-size:12px;")
        self._r_missing.setWordWrap(True)
        self._r_missing.hide()
        lay.addWidget(self._r_missing)
        self._r_result = QTextEdit(); self._r_result.setReadOnly(True)
        self._r_result.setPlaceholderText("接收完成后，文本显示于此（可复制）；文件显示保存路径。")
        self._r_result.setMaximumHeight(120)
        lay.addWidget(self._r_result)
        # 字符统计角标（框内右下角）：当前字符数 / 应接收字符数
        self._r_count = QLabel("0 字符", self._r_result)
        self._r_count.setStyleSheet("color:#64748B; background:rgba(255,255,255,0.85); padding:1px 6px; border-radius:6px; font-size:11px;")
        self._r_result.viewport().installEventFilter(self)
        copy_row = QHBoxLayout(); copy_row.addStretch(1)
        self._r_copy = QPushButton("复制文本"); self._r_copy.clicked.connect(self._copy_result)
        copy_row.addWidget(self._r_copy)
        lay.addLayout(copy_row)
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
            label = f"{len(self._r_result.toPlainText())} 字符" if r.path is None else f"{len(r.data)} 字节"
        elif pipe is not None and pipe.session.manifest is not None:
            # 传输中：文本整包解码后才产生字符数，过程中只能给块恢复进度
            m = pipe.session.manifest
            label = f"{int(round(pipe.progress * m.total_chunks))}/{m.total_chunks} 块"
        else:
            label = "0 字符"
        self._r_count.setText(label)
        self._r_count.adjustSize()
        vp = self._r_result.viewport()
        self._r_count.move(vp.width() - self._r_count.width() - 10, vp.height() - self._r_count.height() - 8)
        self._r_count.raise_()

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
        if event.type() == QEvent.Resize:
            if hasattr(self, "_s_textedit") and obj is self._s_textedit.viewport():
                self._update_s_count()
            elif hasattr(self, "_r_result") and obj is self._r_result.viewport():
                self._update_r_count()
        return super().eventFilter(obj, event)

    # ────────────── 发送 ──────────────
    def _on_send_mode(self):
        is_file = self._s_file.isChecked()
        self._s_pick.setVisible(is_file)
        self._s_filelabel.setVisible(is_file)
        self._s_textedit.setVisible(not is_file)
        if hasattr(self, "_s_clear"):
            self._s_clear.setVisible(not is_file)

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if path:
            self._file_path = path
            name = os.path.basename(path)
            self._s_filelabel.setFullText(name)
            self._s_filelabel.setToolTip(path)

    def _toggle_send(self, checked: bool):
        if checked:
            self._start_send()
        else:
            self._stop_send()

    def _start_send(self):
        if self._s_file.isChecked():
            if not self._file_path or not os.path.isfile(self._file_path):
                QMessageBox.warning(self, "提示", "请先选择有效文件")
                self._s_start.setChecked(False)
                return
            with open(self._file_path, "rb") as f:
                data = f.read()
            filename = os.path.basename(self._file_path)
            ct = ContentType.FILE
        else:
            text = self._s_textedit.toPlainText()
            if not text:
                QMessageBox.warning(self, "提示", "请输入文本")
                self._s_start.setChecked(False)
                return
            data = text.encode("utf-8")
            filename = ""
            ct = ContentType.TEXT
        cfg = SenderConfig(
            ecc_level=self._s_ecc.currentText(),
            grid=self.GRID_OPTIONS[self._s_grid.currentIndex()][1],
            rounds=3,
        )
        sid = random.randint(1, 0xFFFFFFFF)
        self._send_ctrl = SendController(data, ct, filename, sid, cfg)
        self._send_iter = iter(self._send_ctrl)
        self._s_start.setText("停止发送")
        self._send_timer.start(int(1000 / self._s_fps.value()))
        self.statusBar().showMessage(f"发送中：K={self._send_ctrl.K}，{cfg.rounds} 轮")

    def _stop_send(self):
        self._send_timer.stop()
        if self._s_start.isChecked():
            self._s_start.setChecked(False)
        self._s_start.setText("开始发送")

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
        self._qr_label.setPixmap(pix.scaled(self._qr_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

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
    def _pick_save_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self._r_dir.fullText())
        if d:
            self._r_dir.setFullText(d)
            self._r_dir.setToolTip(d)

    def _toggle_recv(self, checked: bool):
        if checked:
            self._start_recv()
        else:
            self._stop_recv()

    def _start_recv(self):
        idx = self._r_cam.value()
        if sys.platform == "win32":
            self._cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        else:
            self._cap = cv2.VideoCapture(idx)
        if not self._cap.isOpened():
            QMessageBox.warning(self, "错误", f"无法打开摄像头 {idx}")
            self._cap = None
            self._r_start.setChecked(False)
            return
        # 链路调优（手机虚拟摄像头尤其关键）：
        # - MJPG 压缩：USB 虚拟摄像头默认 YUYV 未压缩，高分辨率带宽不足会严重掉帧 → 进度慢。
        # - 缓冲 1 帧：默认多帧缓冲会让 read() 返回过时画面，屏幕 QR 快速切换时识别率暴跌。
        # - 固定 720p：在细节清晰度与带宽之间取平衡。set() 失败（设备不支持）静默忽略。
        cap = self._cap
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self._pipe = ReceivePipeline(save_dir=self._r_dir.fullText())
        self._r_progress.setValue(0)
        self._r_result.clear()
        self._update_r_count()
        self._update_r_missing()
        self._r_start.setText("停止接收")
        self._recv_timer.start(int(1000 / self._r_fps.value()))
        self.statusBar().showMessage("接收中…")

    def _stop_recv(self):
        self._recv_timer.stop()
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._r_start.isChecked():
            self._r_start.setChecked(False)
        self._r_start.setText("开始接收")

    def _tick_recv(self):
        if self._cap is None or self._pipe is None:
            return
        ok, frame = self._cap.read()
        if not ok:
            return
        pix = QPixmap.fromImage(_qimage_from_cv(frame))
        self._cam_label.setPixmap(pix.scaled(self._cam_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        try:
            self._pipe.process_image(gray)
        except ValueError as e:
            self._stop_recv()
            QMessageBox.warning(self, "错误", str(e))
            return
        self._r_progress.setValue(int(self._pipe.progress * 100))
        self._update_r_count()
        self._update_r_missing()
        self.statusBar().showMessage(f"进度 {self._pipe.progress:.0%}")
        if self._pipe.result is not None:
            self._on_received()
            self._stop_recv()

    def _on_received(self):
        r = self._pipe.result
        if r.path is None:
            self._r_result.setPlainText(r.data.decode("utf-8", errors="replace"))
            self.statusBar().showMessage("文本接收完成")
        else:
            self._r_result.setPlainText(f"文件已保存：{r.path}")
            self.statusBar().showMessage(f"文件接收完成：{r.path}")
        self._update_r_count()

    def _copy_result(self):
        from PySide6.QtWidgets import QApplication
        text = self._r_result.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage("已复制到剪贴板")

    def closeEvent(self, ev):
        self._stop_send()
        self._stop_recv()
        super().closeEvent(ev)
