"""摄像头「拍屏识别」能力探测 —— 枚举分辨率 × 编码，实测真实出帧与解码可行性。

回答三个真机问题（内置于笔记本的 Integrated Camera 尤其需要）：
1. 这台摄像头最高能给多少分辨率？（决定每个 QR 模块能分到几个像素）
2. set 1920×1080 是「真生效」还是被静默协商回 720p / 640×480？
3. 每个档位实际能跑多少 fps？（USB 2.0 口上高分辨率常掉到个位数）

再顺带算出：在当前帧率/分辨率下，各帧长（700/1200/2200B）的 QR 每模块能分到多少
相机像素——低于 ~5 px/模块 就基本扫不动，这正是「只有 700B 能识别」的量化解释。

运行:
    python tools/diag_screen_scan.py            # 枚举设备 0 的全部模式
    python tools/diag_screen_scan.py 1          # 指定设备索引
"""
from __future__ import annotations

import sys
import time

import cv2

# 帧长 → (源块, QR 版本, 模块数)，与 PC/Android 发送端一致：
# DATA 帧字节 ≈ 144 固定开销 + 源块；模块数由 QR 版本决定（17 + 4×version）
FRAME_MODES = {
    700: (512, 18, 89),
    1200: (1024, 24, 113),
    2200: (2048, 35, 157),
}

RESOLUTIONS = (
    (640, 480),
    (1280, 720),
    (1920, 1080),
    (2560, 1440),
)

# 4 码网格时的每码占比（1×1 时整块画面给一个码，取用户实测的常见取景占比）
SINGLE_COVER = 0.45     # 单码：手机屏在画面里约占 45% 宽
GRID_COVER = 0.22       # 2×2 网格：每个码约占 22% 宽


def _fourcc(name: str) -> int:
    return cv2.VideoWriter_fourcc(*name)


def _fourcc_str(v: int) -> str:
    return "".join(chr((v >> 8 * i) & 0xFF) for i in range(4)).strip("\x00 ")


def measure(cap, seconds: float = 1.2) -> tuple[int, int, float, str]:
    """读一段时间，返回 (真实宽, 真实高, 实测fps, fourcc)。帧尺寸以首帧为准。"""
    w = h = 0
    frames = 0
    fourcc = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        if not w:
            h, w = frame.shape[:2]
            fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        frames += 1
    dt = max(1e-6, time.monotonic() - t0)
    return w, h, frames / dt, _fourcc_str(fourcc)


def px_per_module(frame_w: int, modules: int, cover: float) -> float:
    """码体在画面里占 cover 比例时，每个模块分到多少相机像素。"""
    return frame_w * cover / modules


def probe(index: int) -> None:
    print(f"\n{'='*78}\n设备 [{index}] 能力探测\n{'='*78}")
    best = None
    for fourcc_name in ("MJPG", "YUYV"):
        for (rw, rh) in RESOLUTIONS:
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(index)
            if not cap.isOpened():
                cap.release()
                continue
            try:
                cap.set(cv2.CAP_PROP_FOURCC, _fourcc(fourcc_name))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, rw)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, rh)
                cap.set(cv2.CAP_PROP_FPS, 30)
                aw, ah, fps, fcc = measure(cap)
            finally:
                cap.release()
            if not aw:
                print(f"  {fourcc_name:4} 请求 {rw}×{rh:<5} → 无法出帧")
                continue
            ok_mark = "✓" if (aw >= rw * 0.9 and ah >= rh * 0.9) else "✗"
            print(f"  {fourcc_name:4} 请求 {rw}×{rh:<5} → 实出 {aw}×{ah:<5} {fps:5.1f}fps "
                  f"fourcc={fcc:4} {ok_mark}")
            # 只保留「真正达到请求宽度」且帧率可用的档位作为候选
            if aw >= rw * 0.9 and ah >= rh * 0.9 and fps >= 8:
                score = aw * min(fps, 30)   # 像素吞吐 × 帧率
                if best is None or score > best[0]:
                    best = (score, aw, ah, fps, fourcc_name)

    print(f"\n{'-'*78}")
    if best is None:
        print("没有找到「宽度达标且 ≥8fps」的档位——摄像头能力受限，建议优先用 700B 帧长。")
        return
    _, w, h, fps, name = best
    print(f"推荐采集档位：{w}×{h} @ {fps:.0f}fps {name}")
    print("\n该档位下各帧长的模块像素预算（占比 = 手机屏在画面里的宽度比例）：")
    print(f"  {'帧长':>6} {'模块':>5} {'1×1(45%)':>10} {'2×2(22%)':>10}   可识别性")
    for fb, (chunk, ver, modules) in FRAME_MODES.items():
        p1 = px_per_module(w, modules, SINGLE_COVER)
        p2 = px_per_module(w, modules, GRID_COVER)
        verdict = "稳" if p1 >= 6 else ("勉强" if p1 >= 5 else "很可能扫不出")
        print(f"  {fb:>6} {modules:>5} {p1:>9.1f} {p2:>9.1f}   {verdict}")
    print("\n判读：≥6 px/模块 稳定；5~6 勉强（需对焦准、无强反光）；<5 基本扫不出。")
    print("      若 2200B 落在「扫不出」，改用 700B 并把发送端帧率提到 15~20 补吞吐。")
    print("      想扫 2200B：把手机靠近/放大到画面占比 ~70%，或换支持 1080p 的外接摄像头。")


def main() -> None:
    index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    probe(index)


if __name__ == "__main__":
    main()
