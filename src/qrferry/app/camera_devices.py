"""按 OpenCV 实际索引枚举和打开摄像头设备。"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

__all__ = [
    "CameraDevice",
    "list_camera_devices",
    "open_camera",
    "probe_available_cameras",
    "query_camera_friendly_names",
    "resolve_camera_index",
]


@dataclass(frozen=True)
class CameraDevice:
    index: int
    name: str
    available: bool
    probed: bool = True

    @property
    def label(self) -> str:
        if not self.probed:
            state = "未探测"
        else:
            state = "可用" if self.available else "未打开"
        return f"{self.index} · {self.name} ({state})"


def list_camera_devices(max_index: int = 8, probe: bool = True) -> list[CameraDevice]:
    probe_limit = max_index
    availability = [
        _probe_index(index) if probe else False
        for index in range(probe_limit)
    ]

    devices: list[CameraDevice] = []
    for index, available in enumerate(availability):
        if available or not probe:
            devices.append(CameraDevice(
                index=index,
                name=f"OpenCV Camera {index}",
                available=available,
                probed=probe,
            ))
    if not devices:
        devices.append(CameraDevice(index=0, name="Camera 0", available=False, probed=probe))
    return devices


def resolve_camera_index(value: str | int, max_index: int = 8) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    needle = text.casefold()
    matches = [
        d for d in list_camera_devices(max_index=max_index, probe=True)
        if needle in d.name.casefold() or needle in d.label.casefold()
    ]
    if not matches:
        raise ValueError(f"未找到摄像头设备: {value}")
    available = [d for d in matches if d.available]
    return (available or matches)[0].index


def open_camera(index: int, backend: str = "auto"):
    """按后端策略打开摄像头。

    Windows 上部分虚拟摄像头不支持 DSHOW/MSMF 按索引打开，会打印 OpenCV warning。
    auto 模式优先默认后端，再回退 MSMF 和 DSHOW，兼顾物理摄像头与虚拟摄像头。
    """
    cv2 = _import_cv2()
    candidates = _backend_candidates(cv2, backend)
    old_level = _quiet_cv2(cv2)
    try:
        for api in candidates:
            cap = cv2.VideoCapture(index, api) if api is not None else cv2.VideoCapture(index)
            if cap.isOpened():
                return cap
            cap.release()
    finally:
        _restore_cv2(cv2, old_level)
    return cv2.VideoCapture()


def _probe_index(index: int) -> bool:
    try:
        cap = open_camera(index)
    except ModuleNotFoundError:
        return False
    try:
        return bool(cap.isOpened())
    finally:
        cap.release()


def query_camera_friendly_names() -> list[str]:
    """Windows 上经 PowerShell 尽力枚举相机友好名；非 Windows 或任何失败返回空列表。

    只读系统查询，不引入新依赖；输出顺序与 OpenCV 索引无严格对应关系，仅作显示参考。
    """
    if sys.platform != "win32":
        return []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             ("Get-PnpDevice -Class CAMERA -ErrorAction SilentlyContinue "
              "| Select-Object -ExpandProperty FriendlyName")],
            capture_output=True, text=True, timeout=8, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def probe_available_cameras(max_index: int = 9) -> list[CameraDevice]:
    """真实探测 0..max_index-1：逐个 open_camera 试开，只保留能打开的设备。

    名称优先用系统友好名（按枚举位置尽力对应），取不到时回退 "Camera N"。
    """
    names = query_camera_friendly_names()
    devices: list[CameraDevice] = []
    for index in range(max_index):
        if _probe_index(index):
            name = names[index] if index < len(names) else f"Camera {index}"
            devices.append(CameraDevice(index=index, name=name, available=True))
    return devices


def _import_cv2():
    import cv2
    return cv2


def _backend_candidates(cv2, backend: str) -> list[int | None]:
    normalized = backend.lower()
    if sys.platform != "win32":
        return [None]
    mapping = {
        "msmf": [cv2.CAP_MSMF],
        "dshow": [cv2.CAP_DSHOW],
        "default": [None],
        "auto": [None, cv2.CAP_MSMF, cv2.CAP_DSHOW],
    }
    if normalized not in mapping:
        raise ValueError(f"未知摄像头后端: {backend}")
    return mapping[normalized]


def _quiet_cv2(cv2):
    if hasattr(cv2, "getLogLevel") and hasattr(cv2, "setLogLevel"):
        old_level = cv2.getLogLevel()
        cv2.setLogLevel(0)
        return old_level
    return None


def _restore_cv2(cv2, old_level) -> None:
    if old_level is not None:
        cv2.setLogLevel(old_level)
