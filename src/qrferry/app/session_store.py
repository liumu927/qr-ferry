"""接收会话断点续传持久化 —— app 层 JSON 落盘。

core 层（ReceiveSession）提供纯数据快照，本模块仅负责 IO（读写 JSON）。
文件损坏按「坏 payload 静默丢弃」原则（协议 §11 安全模型）：解析失败视为无可恢复会话。
"""
from __future__ import annotations

import json
import os
from typing import Optional

from qrferry.core.session import ReceiveSession

__all__ = ["save", "load", "clear"]

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


def load(save_dir: str) -> Optional[ReceiveSession]:
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
