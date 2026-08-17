"""针对单设备交叉诊断：后端 × set 组合 → 实测 read 出帧。

定位「EV 虚拟摄像头 isOpened 成功但 read 失败」究竟由哪个因素触发：
- 是否 set MJPG/720/60 才坏（参数协商失败）→ 裸 read 应正常
- 还是宿主根本没传帧（手机端 EV 未在前台/断连）→ 裸 read 也失败
- 换 DSHOW 后端能否救回

运行: python tools/diag_capture_formats.py [index=0]
"""
from __future__ import annotations

import sys
import time

import cv2

MJPG = cv2.VideoWriter_fourcc(*"MJPG")


def _fourcc_str(fourcc: int) -> str:
    return "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4))


def _open(index: int, backend: str):
    if backend == "MSMF":
        return cv2.VideoCapture(index, cv2.CAP_MSMF)
    if backend == "DSHOW":
        return cv2.VideoCapture(index, cv2.CAP_DSHOW)
    return cv2.VideoCapture(index)   # 默认 = Windows MSMF


def test(index: int, backend: str, label: str, sets: list) -> None:
    cap = _open(index, backend)
    if not cap.isOpened():
        print(f"  [{backend:6}|{label:20}] open 失败")
        return
    for s in sets:
        s(cap)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fc = int(cap.get(cv2.CAP_PROP_FOURCC))
    n, t0 = 0, time.monotonic()
    for _ in range(10):
        ok, _ = cap.read()
        if ok:
            n += 1
    dt = max(1e-6, time.monotonic() - t0)
    print(f"  [{backend:6}|{label:20}] {w}x{h} FOURCC={_fourcc_str(fc)!r} "
          f"read {n}/10 ~{n/dt:.1f}fps")
    cap.release()


def main() -> None:
    index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print(f"=== 设备 [{index}] 后端 × set 组合诊断 ===")
    app_sets = [
        lambda c: c.set(cv2.CAP_PROP_FOURCC, MJPG),
        lambda c: c.set(cv2.CAP_PROP_FRAME_WIDTH, 1280),
        lambda c: c.set(cv2.CAP_PROP_FRAME_HEIGHT, 720),
        lambda c: c.set(cv2.CAP_PROP_FPS, 60),
    ]
    wh_sets = [
        lambda c: c.set(cv2.CAP_PROP_FRAME_WIDTH, 1280),
        lambda c: c.set(cv2.CAP_PROP_FRAME_HEIGHT, 720),
    ]
    print("-- 默认(MSMF) 后端 --")
    test(index, "default", "不set", [])
    test(index, "default", "只设720p", wh_sets)
    test(index, "default", "复刻app(MJPG+720+60)", app_sets)
    print("-- DSHOW 后端 --")
    test(index, "DSHOW", "不set", [])
    test(index, "DSHOW", "复刻app(MJPG+720+60)", app_sets)


if __name__ == "__main__":
    main()
