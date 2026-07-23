"""按 OpenCV 实际索引枚举和打开摄像头设备。"""
from __future__ import annotations

import sys
from dataclasses import dataclass

__all__ = ["CameraDevice", "list_camera_devices", "open_camera", "resolve_camera_index"]


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
