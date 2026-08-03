"""标准 QR 参数寻优基准 —— 仿真"手机屏幕 → 相机"链路的好帧率。

用途：比较不同 帧长(max_frame_bytes) × 纠错级 × 发送帧率 组合下的
有效吞吐估计，为 GUI 参数选择提供依据。结果是相对排序，不是绝对承诺。

仿真链路（对每帧）：
  SendController 产出 DATA 帧 → segno 编码 QR（720×720，对齐安卓端）
  → 缩放到相机视野内 ~550px → 放在 1280×720 灰底画布
  → 以 p_straddle = fps_send × exposure 概率与下一帧 50/50 混合（时间混帧）
  → 高斯模糊 + 传感器噪声 → zxing-cpp 解码

直接运行: python tools/qr_loopback_bench.py [--frames 60] [--seed 1]
"""
from __future__ import annotations

import argparse
import random
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from qrferry.app.send_controller import SendController, SenderConfig  # noqa: E402
from qrferry.core.frame import ContentType  # noqa: E402
from qrferry.qr.backend import StandardQrBackend  # noqa: E402

CAM_W, CAM_H = 1280, 720     # 相机分辨率（接收端固定 720p）
CODE_PX = 550                # 码在相机画面中的边长（手机近距离占画面高度大头）
BOX_SIZE = 8                 # segno scale，720px 位图对齐安卓 QrRenderer


def make_frames(data: bytes, chunk_size_log: int, max_frame_bytes: int,
                ecc: str, n: int) -> list[bytes]:
    ctrl = SendController(
        data, ContentType.FILE, "bench.bin", session_id=0xBEEF,
        config=SenderConfig(chunk_size_log=chunk_size_log,
                            max_frame_bytes=max_frame_bytes, ecc_level=ecc,
                            redundancy=1.0, rounds=1),
    )
    frames = []
    it = ctrl.data_frames() if hasattr(ctrl, "data_frames") else iter(ctrl)
    for raw in it:
        frames.append(raw)
        if len(frames) >= n:
            break
    return frames


def render(backend: StandardQrBackend, frame: bytes, ecc: str) -> np.ndarray:
    img = backend.encode(frame, ecc_level=ecc, box_size=BOX_SIZE, border=2)
    arr = np.array(img)
    return cv2.resize(arr, (CODE_PX, CODE_PX), interpolation=cv2.INTER_NEAREST)


def camera_capture(code: np.ndarray, next_code: np.ndarray | None,
                   mix: bool, rng: np.random.Generator) -> np.ndarray:
    if mix and next_code is not None:
        code = (code.astype(np.float32) * 0.5 + next_code.astype(np.float32) * 0.5)
        code = code.astype(np.uint8)
    canvas = np.full((CAM_H, CAM_W), 200, np.uint8)
    y0, x0 = (CAM_H - CODE_PX) // 2, (CAM_W - CODE_PX) // 2
    canvas[y0:y0 + CODE_PX, x0:x0 + CODE_PX] = code
    canvas = cv2.GaussianBlur(canvas, (3, 3), 0)
    noise = rng.normal(0, 4, canvas.shape)
    return np.clip(canvas.astype(np.float32) + noise, 0, 255).astype(np.uint8)


def bench_config(data: bytes, chunk_size_log: int, max_frame_bytes: int,
                 ecc: str, fps: int, n_frames: int, seed: int,
                 exposure_s: float) -> dict:
    backend = StandardQrBackend()
    frames = make_frames(data, chunk_size_log, max_frame_bytes, ecc, n_frames + 1)
    rng = np.random.default_rng(seed)
    p_straddle = min(1.0, fps * exposure_s)
    codes = [render(backend, f, ecc) for f in frames]
    good = 0
    decode_ms = 0.0
    for i, code in enumerate(codes[:-1]):
        mix = rng.random() < p_straddle
        img = camera_capture(code, codes[i + 1], mix, rng)
        t0 = time.perf_counter()
        decoded = backend.decode(img)
        decode_ms += (time.perf_counter() - t0) * 1000
        if decoded and decoded[0] == frames[i]:
            good += 1
    total = len(codes) - 1
    payload = int(np.median([len(f) for f in frames]))
    good_rate = good / total
    effective_kbs = good_rate * fps * payload / 1024
    return {
        "chunk_log": chunk_size_log, "frame_bytes": max_frame_bytes, "ecc": ecc,
        "fps": fps, "payload": payload, "good_rate": good_rate,
        "decode_ms": decode_ms / total, "effective_kbs": effective_kbs,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=60, help="每个组合仿真的帧数")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--kb", type=int, default=256, help="源数据大小(KB，不可压缩)")
    ap.add_argument("--exposure-ms", type=float, default=33.0,
                    help="相机曝光时间 ms（30fps≈33ms；60fps 摄像头≈16ms）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    data = random.Random(args.seed).getrandbits(args.kb * 1024 * 8).to_bytes(
        args.kb * 1024, "big")
    zlib.compress(data, 9)   # 预热，确保 zlib 可用
    exposure_s = args.exposure_ms / 1000

    print(f"仿真：{args.kb}KB 不可压缩数据，每组合 {args.frames} 帧，"
          f"混帧概率 = fps × {args.exposure_ms:.0f}ms 曝光\n")
    header = (f"{'帧长':>5} {'源块':>5} {'ECC':>3} {'fps':>3} {'payload':>7} "
              f"{'好帧率':>7} {'解码ms':>7} {'有效KB/s':>8}")
    print(header)
    print("-" * len(header))
    # (chunk_size_log, max_frame_bytes) 联动组合：700→512B、1200→1024B、2200→2048B
    for chunk_log, frame_bytes in ((9, 700), (10, 1200), (11, 2200)):
        for ecc in ("L", "M"):
            for fps in (12, 20):
                r = bench_config(data, chunk_log, frame_bytes, ecc, fps,
                                 args.frames, args.seed, exposure_s)
                print(f"{r['frame_bytes']:>5} {1 << r['chunk_log']:>5} {r['ecc']:>3} "
                      f"{r['fps']:>3} {r['payload']:>7} {r['good_rate']:>7.1%} "
                      f"{r['decode_ms']:>7.1f} {r['effective_kbs']:>8.1f}")
    print("\n注：有效KB/s = 好帧率 × fps × 中位帧字节；"
          "典型可压缩文件还需乘以压缩率倒数（如 .doc ≈ ×6）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
