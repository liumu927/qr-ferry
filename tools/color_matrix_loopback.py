from __future__ import annotations

import argparse
import hashlib
import random
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qrferry.app.send_controller import (  # noqa: E402
    COLOR_MATRIX_CHUNK_SIZE_LOG,
    COLOR_MATRIX_MAX_FRAME_BYTES,
    SendController,
    SenderConfig,
)
from qrferry.core.frame import ContentType, ProtocolError, decode_frame  # noqa: E402
from qrferry.core.session import ReceiveSession  # noqa: E402
from qrferry.qr.color_matrix import ColorMatrixBackend  # noqa: E402


def _load_payload(args: argparse.Namespace) -> tuple[bytes, int, str]:
    if args.file:
        path = Path(args.file)
        return path.read_bytes(), ContentType.FILE, path.name
    return args.text.encode("utf-8"), ContentType.TEXT, ""


def run(args: argparse.Namespace) -> int:
    data, content_type, filename = _load_payload(args)
    backend = ColorMatrixBackend()
    config = SenderConfig(
        chunk_size_log=COLOR_MATRIX_CHUNK_SIZE_LOG,
        max_frame_bytes=COLOR_MATRIX_MAX_FRAME_BYTES,
        rounds=args.rounds,
        grid=(1, 1),
    )
    ctrl = SendController(
        data,
        content_type,
        filename,
        session_id=args.session_id,
        config=config,
    )
    sess = ReceiveSession()
    valid = 0
    bad = 0
    shown_progress = -1

    for frame_index, frame in enumerate(ctrl, start=1):
        decoded = backend.decode(backend.encode(frame))
        if not decoded:
            bad += 1
            continue
        for raw in decoded:
            try:
                header, payload = decode_frame(raw)
            except ProtocolError:
                bad += 1
                continue
            sess.ingest(header, payload)
            valid += 1
        pct = int(sess.progress * 100)
        if args.verbose and pct != shown_progress:
            shown_progress = pct
            print(f"progress={pct}% valid={valid} bad={bad} frame={frame_index}")
        if sess.is_complete:
            break

    if not sess.is_complete:
        print(
            f"FAIL incomplete progress={sess.progress:.1%} "
            f"K={sess.K} valid={valid} bad={bad}",
            file=sys.stderr,
        )
        return 2

    restored = zlib.decompress(sess.reassemble())
    if restored != data:
        print("FAIL payload mismatch", file=sys.stderr)
        return 3

    print(
        "OK "
        f"bytes={len(data)} K={ctrl.K} valid={valid} bad={bad} "
        f"sha256={hashlib.sha256(restored).hexdigest()}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Color matrix encode/decode loopback without camera.")
    parser.add_argument("--text", default="qr-ferry color matrix loopback")
    parser.add_argument("--file", help="Send a file instead of --text.")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--session-id", type=int, default=random.randint(1, 0x7FFFFFFF))
    parser.add_argument("--verbose", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
