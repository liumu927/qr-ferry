# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('segno')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('zxingcpp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['scripts\\run_app.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtQuick3D', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtNetwork', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtDesigner', 'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtPdf', 'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning', 'PySide6.QtSensors', 'PySide6.QtSerialPort'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='qr-ferry',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='qr-ferry',
)
