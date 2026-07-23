from __future__ import annotations

import argparse
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import ImageGrab  # noqa: E402

from qrferry.core.frame import FrameType, ProtocolError, decode_frame  # noqa: E402
from qrferry.core.session import ReceiveSession  # noqa: E402
from qrferry.qr.color_matrix import ColorMatrixBackend  # noqa: E402


def run(args: argparse.Namespace) -> int:
    backend = ColorMatrixBackend()
    sess = ReceiveSession()
    valid = 0
    bad = 0
    types: dict[int, int] = {}
    shown_progress = -1
    deadline = time.monotonic() + args.seconds
    interval = 1.0 / max(1, args.fps)
    dump_dir = Path(args.dump_dir) if args.dump_dir else None
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
    frame_index = 0

    while time.monotonic() < deadline:
        frame_index += 1
        frame = ImageGrab.grab()
        if dump_dir is not None and (frame_index == 1 or args.dump_all):
            frame.save(dump_dir / f"screen_{frame_index:04d}.png")
        decoded = backend.decode(frame)
        if not decoded:
            time.sleep(interval)
            continue
        if dump_dir is not None:
            frame.save(dump_dir / f"screen_decoded_{frame_index:04d}.png")
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
            print(f"progress={pct}% K={sess.K} valid={valid} bad={bad} types={_format_types(types)}")
        if sess.is_complete:
            restored = zlib.decompress(sess.reassemble())
            if args.output:
                Path(args.output).write_bytes(restored)
                print(f"OK saved={args.output} bytes={len(restored)} valid={valid} bad={bad} types={_format_types(types)}")
            else:
                print(f"OK bytes={len(restored)} valid={valid} bad={bad} types={_format_types(types)}")
            return 0
        time.sleep(interval)

    print(
        f"TIMEOUT progress={sess.progress:.1%} K={sess.K} valid={valid} bad={bad} types={_format_types(types)}",
        file=sys.stderr,
    )
    return 2


def _format_types(types: dict[int, int]) -> str:
    names = {
        int(FrameType.MANIFEST): "MANIFEST",
        int(FrameType.DATA): "DATA",
        int(FrameType.END): "END",
    }
    return ",".join(f"{names.get(k, hex(k))}:{v}" for k, v in sorted(types.items())) or "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="Receive color matrix frames from screen capture.")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--output", help="Write restored payload bytes to this file.")
    parser.add_argument("--dump-dir", help="Save captured frames for diagnostics.")
    parser.add_argument("--dump-all", action="store_true", help="Save every captured frame when --dump-dir is set.")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
