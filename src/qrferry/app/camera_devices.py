"""按 OpenCV 实际索引枚举和打开摄像头设备。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass

__all__ = [
    "CameraDevice",
    "list_camera_devices",
    "open_camera",
    "probe_available_cameras",
    "probe_available_cameras_fresh",
    "query_camera_friendly_names",
    "resolve_camera_index",
]

# 探测子进程逐索引×逐后端试开，慢设备（指纹/红外相机协商 3-5s）下 9 个索引
# 可能到 30s+；超时只放弃子进程结果回退进程内探测，不阻塞刷新。
_PROBE_SUBPROC_TIMEOUT = 90


@dataclass(frozen=True)
class CameraDevice:
    index: int
    name: str
    available: bool
    probed: bool = True
    # 探测时实际可用的后端（"auto"/"msmf"/"dshow"）：采集打开时优先用同一后端。
    # 热插拔场景下 MSMF 设备枚举可能陈旧（先开软件后插摄像头时刷新也找不到），
    # 逐后端独立探测能兜住「MSMF 看不到但 DSHOW 看得到」的新设备。
    backend: str = "auto"

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


def _probe_index(index: int, backend: str = "auto") -> bool:
    try:
        cap = open_camera(index, backend=backend)
    except ModuleNotFoundError:
        return False
    try:
        if not cap.isOpened():
            return False
        # 必须实测 read 出帧：部分虚拟设备 isOpened() 成功但 read 永久失败
        # （MSMF 协商错误），仅验 isOpened 会把这类「假可用」设备列入下拉框，
        # 选中后 _capture_loop 静默黑屏卡死。read 一帧即可甄别。
        ok, _ = cap.read()
        return bool(ok)
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


def probe_available_cameras_fresh(max_index: int = 9) -> list[CameraDevice] | None:
    """在全新子进程中探测：绕开 OpenCV 进程内陈旧的设备枚举缓存。

    Windows 上 MSMF/DSHOW 的设备枚举在进程内有缓存——应用启动后才插入的
    外接摄像头，主进程内重试再多次也可能永远「看不见」（社区通用解法就是
    重启进程）。子进程每次都是全新枚举，天然免疫该缓存。

    子进程命令行（frozen 即 exe 自身；源码运行时为 python + run_app.py）：
        qr-ferry.exe --probe-cameras <max_index> <out_json_path>
    结果写临时文件而非 stdout——`--windowed` 打包的进程没有控制台，
    sys.stdout 为 None，stdout 回传不可靠。任何失败（找不到入口/超时/
    非零退出/JSON 损坏）返回 None，由调用方回退进程内探测。
    """
    entry = _probe_entry_command()
    if entry is None:
        return None
    fd, out_path = tempfile.mkstemp(suffix=".json", prefix="qrferry-probe-")
    os.close(fd)
    try:
        try:
            proc = subprocess.run(
                [*entry, "--probe-cameras", str(max_index), out_path],
                capture_output=True, timeout=_PROBE_SUBPROC_TIMEOUT, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        try:
            with open(out_path, encoding="utf-8") as fh:
                rows = json.load(fh)
            return [
                CameraDevice(
                    index=int(r["index"]), name=str(r["name"]),
                    available=True, probed=True, backend=str(r.get("backend", "auto")))
                for r in rows
            ]
        except (OSError, ValueError, KeyError, TypeError):
            return None
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _probe_entry_command() -> list[str] | None:
    """探测子进程的启动命令：frozen 为 exe 自身；源码为 python + run_app.py。"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    # 源码运行：src/qrferry/app/camera_devices.py → 仓库根/scripts/run_app.py
    run_app = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "scripts", "run_app.py"))
    if os.path.isfile(run_app):
        return [sys.executable, run_app]
    return None


def _probe_cameras_main(argv: list[str]) -> int:
    """`--probe-cameras` 子进程入口：探测并把 JSON 写到 argv 指定路径。"""
    max_index = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 9
    out_path = argv[2] if len(argv) > 2 else None
    devices = probe_available_cameras(max_index=max_index)
    rows = [{"index": d.index, "name": d.name, "backend": d.backend} for d in devices]
    payload = json.dumps(rows, ensure_ascii=False)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
    return 0


def _is_probe_cameras_request(argv: list[str] | None = None) -> bool:
    """判断当前进程是否以 `--probe-cameras` 探测模式启动（供入口早退）。"""
    return "--probe-cameras" in (argv if argv is not None else sys.argv)


def probe_available_cameras(max_index: int = 9) -> list[CameraDevice]:
    """真实探测 0..max_index-1：逐后端独立试开，只保留能开且能出帧的设备。

    每个索引按 auto → msmf → dshow 依次独立判定（不是 open_camera 的"第一个
    打开即停"）：MSMF 枚举陈旧时（先开软件后插摄像头）auto 可能命中一个
    isOpened 成功但 read 失效的陈旧项，此时 DSHOW 仍能试到真实设备——避免
    热插拔设备「点击刷新也找不到」。backend 记录实际可用的后端，供采集打开复用。

    名称优先用系统友好名（按枚举位置尽力对应），取不到时回退 "Camera N"。
    """
    names = query_camera_friendly_names()
    devices: list[CameraDevice] = []
    for index in range(max_index):
        for backend in ("auto", "msmf", "dshow"):
            if _probe_index(index, backend):
                name = names[index] if index < len(names) else f"Camera {index}"
                devices.append(CameraDevice(
                    index=index, name=name, available=True, backend=backend))
                break
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
