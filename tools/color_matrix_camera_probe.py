from __future__ import annotations

import argparse
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

from qrferry.core.frame import FrameType, ProtocolError, decode_frame  # noqa: E402
from qrferry.core.session import ReceiveSession  # noqa: E402
from qrferry.app.camera_devices import list_camera_devices, open_camera, resolve_camera_index  # noqa: E402
from qrferry.qr.color_matrix import ColorMatrixBackend  # noqa: E402


def run(args: argparse.Namespace) -> int:
    if args.list_cameras:
        for device in list_camera_devices(max_index=args.max_camera_index + 1, probe=args.probe):
            print(device.label)
        return 0
    cap = _open_camera(args)
    if not cap.isOpened():
        print(f"ERROR cannot open camera {args.camera}", file=sys.stderr)
        return 1

    backend = ColorMatrixBackend()
    sess = ReceiveSession()
    valid = 0
    bad = 0
    frames = 0
    types: dict[int, int] = {}
    shown_progress = -1
    interval = 1.0 / max(1, args.fps)
    deadline = time.monotonic() + args.seconds
    dump_dir = Path(args.dump_dir) if args.dump_dir else None
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)

    try:
        while time.monotonic() < deadline:
            ok, bgr = cap.read()
            if not ok:
                time.sleep(interval)
                continue
            frames += 1
            if dump_dir is not None and (frames == 1 or args.dump_all):
                cv2.imwrite(str(dump_dir / f"camera_{frames:04d}.png"), bgr)

            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            decoded = backend.decode(rgb)
            if not decoded:
                time.sleep(interval)
                continue
            if dump_dir is not None:
                cv2.imwrite(str(dump_dir / f"camera_decoded_{frames:04d}.png"), bgr)

            for raw in decoded:
                try:
                    header, payload = decode_frame(raw)
                except ProtocolError:
                    bad += 1
                    continue
                types[header.frame_type] = types.get(header.frame_type, 0) + 1
                sess.ingest(header, payload)
                valid += 1

            pct = int(sess.progress * 100)
            if pct != shown_progress:
                shown_progress = pct
                print(
                    f"progress={pct}% K={sess.K} frames={frames} "
                    f"valid={valid} bad={bad} types={_format_types(types)}"
                )
            if sess.is_complete:
                restored = zlib.decompress(sess.reassemble())
                if args.output:
                    Path(args.output).write_bytes(restored)
                    print(
                        f"OK saved={args.output} bytes={len(restored)} frames={frames} "
                        f"valid={valid} bad={bad} types={_format_types(types)}"
                    )
                else:
                    print(
                        f"OK bytes={len(restored)} frames={frames} "
                        f"valid={valid} bad={bad} types={_format_types(types)}"
                    )
                return 0
            time.sleep(interval)
    finally:
        cap.release()

    print(
        f"TIMEOUT progress={sess.progress:.1%} K={sess.K} frames={frames} "
        f"valid={valid} bad={bad} types={_format_types(types)}",
        file=sys.stderr,
    )
    return 2


def _open_camera(args: argparse.Namespace):
    index = resolve_camera_index(args.camera, max_index=args.max_camera_index + 1)
    cap = open_camera(index, backend=args.backend)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    return cap


def _format_types(types: dict[int, int]) -> str:
    names = {
        int(FrameType.MANIFEST): "MANIFEST",
        int(FrameType.DATA): "DATA",
        int(FrameType.END): "END",
    }
    return ",".join(f"{names.get(k, hex(k))}:{v}" for k, v in sorted(types.items())) or "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe color matrix frames from a real camera.")
    parser.add_argument("--camera", default="0", help="Camera index or device-name keyword.")
    parser.add_argument("--backend", default="auto", choices=("auto", "msmf", "dshow", "default"),
                        help="OpenCV camera backend on Windows.")
    parser.add_argument("--list-cameras", action="store_true", help="List detected camera devices and exit.")
    parser.add_argument("--probe", action="store_true", help="Open indices while listing to verify availability.")
    parser.add_argument("--max-camera-index", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--output", help="Write restored payload bytes to this file.")
    parser.add_argument("--dump-dir", help="Save camera frames for diagnostics.")
    parser.add_argument("--dump-all", action="store_true", help="Save every captured frame when --dump-dir is set.")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
