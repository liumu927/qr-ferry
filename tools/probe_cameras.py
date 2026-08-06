"""摄像头能力探测 —— 实测设备协商的 FOURCC/分辨率/FPS/read 帧率，定位链路瓶颈。

对比高拍仪 vs 手机摄像头当相机时的真实参数，回答"为何高拍仪扫描效率低"：
- set MJPG 是否真生效（不支持则回退 YUYV 未压缩，USB 2.0 带宽不足致帧率暴跌）
- 实际帧率（get CAP_PROP_FPS 不可信，用连续 read 计时实测）
- 首帧延迟（曝光/对焦收敛耗时）

直接运行: python tools/probe_cameras.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402


def _fourcc_str(fourcc: int) -> str:
    return "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4))


def _snapshot(cap, label: str) -> None:
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    print(f"    {label}: {w}x{h} @ {fps:.1f}fps  FOURCC={_fourcc_str(fourcc)!r}")


def probe(index: int, read_frames: int = 20) -> None:
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        return
    print(f"[{index}] 打开成功")
    try:
        _snapshot(cap, "默认         ")
        # 复刻 _capture_loop 的链路调优参数
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS, 60)
        _snapshot(cap, "set MJPG/720/60")

        # 首帧延迟（曝光/对焦收敛 + 驱动预热）
        t0 = time.monotonic()
        ok, _ = cap.read()
        first = time.monotonic() - t0
        # 连续 read 计时实测帧率（get CAP_PROP_FPS 不可信）
        n = 0
        t1 = time.monotonic()
        for _ in range(read_frames):
            ok, _ = cap.read()
            if ok:
                n += 1
        dt = max(1e-6, time.monotonic() - t1)
        print(f"    首帧 {first*1000:.0f}ms | read {n}/{read_frames} 帧用时 {dt:.2f}s → 实际 ~{n/dt:.1f} fps")
    finally:
        cap.release()


def main() -> None:
    print("=== 摄像头能力探测 ===")
    found = False
    for i in range(4):
        cap = cv2.VideoCapture(i)
        opened = cap.isOpened()
        cap.release()
        if opened:
            found = True
            probe(i)
    if not found:
        print("未发现可打开的摄像头")


if __name__ == "__main__":
    main()
