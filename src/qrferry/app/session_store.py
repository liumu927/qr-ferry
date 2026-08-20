"""接收会话断点续传持久化 —— app 层 JSON 落盘。

core 层（ReceiveSession）提供纯数据快照，本模块仅负责 IO（读写 JSON）。
文件损坏按「坏 payload 静默丢弃」原则（协议 §11 安全模型）：解析失败视为无可恢复会话。
"""
from __future__ import annotations

import json
import os

from qrferry.app.send_controller import SendController
from qrferry.core.session import ReceiveSession

__all__ = ["clear", "clear_sender", "load", "load_sender", "save", "save_sender"]

_DIR_NAME = ".qrferry"
_PENDING_FILE = "pending.json"


def _state_dir(save_dir: str) -> str:
    return os.path.join(save_dir, _DIR_NAME)


def _pending_path(save_dir: str) -> str:
    return os.path.join(_state_dir(save_dir), _PENDING_FILE)


def save(session: ReceiveSession, save_dir: str) -> None:
    """持久化未完成会话；无可快照的会话（MANIFEST 未到）则清除旧文件。"""
    snap = session.to_snapshot()
    os.makedirs(_state_dir(save_dir), exist_ok=True)
    if snap is None:
        clear(save_dir)
        return
    with open(_pending_path(save_dir), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)


def load(save_dir: str) -> ReceiveSession | None:
    """读取未完成会话；无文件或损坏返回 None。"""
    path = _pending_path(save_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        return ReceiveSession.from_snapshot(snap)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None   # 损坏：静默丢弃


def clear(save_dir: str) -> None:
    """删除未完成会话文件（传输完成后调用）。"""
    try:
        os.remove(_pending_path(save_dir))
    except FileNotFoundError:
        pass


# ── 发送端断点续传 ───────────────────────────────────────
_SENDER_FILE = "sender.json"


def _sender_path(save_dir: str) -> str:
    return os.path.join(_state_dir(save_dir), _SENDER_FILE)


def save_sender(ctrl: SendController, save_dir: str) -> None:
    """持久化发送端快照；TEXT 或已优化 FILE 同时把发送字节落盘以便重启还原。

    - TEXT：原始文本字节落 `send_<sid>.bin`（发送数据即原始文本）；
    - 已优化 FILE：优化后的字节落 `send_<sid>.opt`（发送数据即优化结果，
      续传时直接复用，不再对原文件重复优化——优化器输出依赖 Pillow/zlib 版本，
      二次优化可能产生不同字节导致 SHA 校验失败）。
    """
    snap = ctrl.to_snapshot()
    os.makedirs(_state_dir(save_dir), exist_ok=True)
    is_text = snap.get("source_kind") == "text"
    if is_text or snap.get("optimized"):
        ext = ".bin" if is_text else ".opt"
        bin_path = os.path.join(_state_dir(save_dir), f"send_{snap['session_id']}{ext}")
        with open(bin_path, "wb") as f:
            f.write(ctrl.raw_data)
        snap["source_path"] = bin_path
    with open(_sender_path(save_dir), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)


def load_sender(save_dir: str) -> SendController | None:
    """读取发送端快照重建 SendController；无文件/损坏/源失效返回 None。"""
    path = _sender_path(save_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        return SendController.from_snapshot(snap)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def clear_sender(save_dir: str) -> None:
    """清理发送端快照与 TEXT/优化 sidecar（发送完成或放弃后续传时调用）。"""
    import glob
    try:
        os.remove(_sender_path(save_dir))
    except FileNotFoundError:
        pass
    for p in glob.glob(os.path.join(_state_dir(save_dir), "send_*.bin")) + \
             glob.glob(os.path.join(_state_dir(save_dir), "send_*.opt")):
        try:
            os.remove(p)
        except OSError:
            pass
