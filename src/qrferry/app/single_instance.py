"""单实例锁：同一时刻只允许一个 qr-ferry 进程存活。

Windows 上用会话级命名互斥体判定「是否已有实例」；后启动的实例退出前经一条
注册窗口消息通知首实例把窗口从托盘恢复并置顶——用户双击 exe 的预期是
「打开软件」，而不是凭空多出一个进程。

关闭按钮只最小化到托盘（进程仍在），若没有这把锁，再次双击 exe 会真的再开
一个进程。锁按固定名（与版本无关），避免新旧版本同时存活。

非 Windows 平台退化为不设防（is_first_instance 恒 True、notify 空操作）。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_MUTEX_NAME = "qr-ferry-single-instance"   # 会话级命名互斥体（与版本无关）
_SHOW_MSG_NAME = "QR_FERRY_SHOW_WINDOW"    # 自定义窗口消息：请首实例显示主窗口
_WINDOW_TITLE_PREFIX = "qr-ferry"          # 主窗口标题前缀（EnumWindows 匹配用）

_ERROR_ALREADY_EXISTS = 183

_handle: int | None = None     # 互斥体句柄：进程存续期间持有，退出即释放
_show_msg: int | None = None   # 注册后的窗口消息号

if sys.platform == "win32":
    # use_last_error=True：ctypes 内部调用不会污染线程 last-error，
    # GetLastError 语义与 Win32 一致（CreateMutexW 后立即读取）。
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE

    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT

    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostMessageW.restype = wintypes.BOOL

    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int

    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int

    _user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    _user32.EnumWindows.restype = wintypes.BOOL


def is_first_instance() -> bool:
    """尝试获取命名互斥体；True 表示本进程是第一个实例（可继续启动）。"""
    global _handle
    if sys.platform != "win32":
        return True
    try:
        handle = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    except (AttributeError, OSError):
        return True   # 拿不到句柄（权限等）：不阻断启动，退化为允许多实例
    if not handle:
        return True
    _handle = int(handle)
    return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def register_show_message() -> int | None:
    """注册「显示窗口」窗口消息号；首实例在 nativeEvent 中监听该消息。

    第二实例经 PostMessage 发送此消息（无需前台权限），首实例收到后恢复窗口。
    """
    global _show_msg
    if sys.platform != "win32":
        return None
    if _show_msg is None:
        try:
            _show_msg = int(_user32.RegisterWindowMessageW(_SHOW_MSG_NAME))
        except (AttributeError, OSError):
            _show_msg = 0
    return _show_msg or None


def _find_main_hwnd() -> int | None:
    """按标题前缀查找主窗口 HWND；找不到返回 None。"""
    result: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.startswith(_WINDOW_TITLE_PREFIX):
            result.append(int(hwnd))
            return False   # 找到即停
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except (AttributeError, OSError):
        return None
    return result[0] if result else None


def notify_existing_instance() -> None:
    """第二实例：通知首实例显示窗口，然后本进程应立即退出。"""
    if sys.platform != "win32":
        return
    hwnd = _find_main_hwnd()
    msg = register_show_message()
    if hwnd is None or msg is None:
        return
    try:
        _user32.PostMessageW(hwnd, msg, 0, 0)
    except (AttributeError, OSError):
        pass
