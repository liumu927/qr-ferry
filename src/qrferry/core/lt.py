"""LT 喷泉码 —— 协议 v1.0 §6。

外层前向纠错：对 K 个定长源块无限生成编码符号，接收端收齐略多于 K 个不同符号
即可整体恢复，天然适配无序、丢包、重复的单向光学信道。

确定性问题：同一 (session_id, symbol_id) 恒等映射到同一 (degree, adjacency, xor_data)。
注意 —— degree/adjacency 在 DATA 帧中显式传输，解码端无需 PRNG，故 PRNG 跨语言
一致性非必须；PRNG 仅用于发送端补传重放时的符号复现。

实测开销（peeling decoder）：小 K（≤256）约需 K×1.8 个符号、大 K（≥10⁴）趋近
K×1.2，理论 RSD 渐近值为 K×1.1。发送端按 K×2.0 兜底播放；详见协议 §6.2/§12。
"""
from __future__ import annotations

import bisect
import math
import random
from collections import deque

__all__ = ["DIST_RSD", "DIST_ISD", "DIST_DEGENERATE", "LtEncoder", "LtDecoder"]

# 度分布类型（与 frame.LtDistribution 取值对齐：RSD=0, ISD=1, DEGENERATE=2）
DIST_RSD = 0
DIST_ISD = 1
DIST_DEGENERATE = 2

# 单 DATA 帧字节安全上限。QR version 40 字节模式容量：L2953 / M2331 / Q1663 / H1273；
# 取 1200B 适配所有 ECC 级（含 H=1273）并留识别余量。degree 过大会使 ADJACENCY(K×4B)
# 撑爆单帧容量，故编码端需限上限。
SAFE_FRAME_BYTES = 1200

# degree 上限（识别约束）。即便帧上限允许更大 degree，超过此值时 ADJACENCY 过长使 QR
# version 偏高、模块过密，摄像头识别率显著下降。30 让 QR 保持 v14-17（模块大、好识别）。
MAX_DEGREE = 30

_RSD_C = 0.1
_RSD_DELTA = 0.5


# ── 度分布 ───────────────────────────────────────────────
def _build_degree_cdf(K: int, dist: int, c: float = _RSD_C, delta: float = _RSD_DELTA) -> list[float]:
    """构建度分布累积分布函数 cdf，cdf[d] = P(degree <= d)，长度 K+1。"""
    if K <= 0:
        return [0.0]
    rho = [0.0] * (K + 1)
    rho[1] = 1.0 / K
    for d in range(2, K + 1):
        rho[d] = 1.0 / (d * (d - 1))

    if dist == DIST_ISD:
        mu = rho
    elif dist == DIST_RSD:
        R = c * math.log(K / delta) * math.sqrt(K)
        tau = [0.0] * (K + 1)
        for d in range(1, K):
            tau[d] = R / (d * K)
        tau[K] = (R * math.log(R / delta) / K) if (R / delta) > 1 else 0.0
        Z = sum(rho[d] + tau[d] for d in range(1, K + 1))
        mu = [0.0] * (K + 1)
        for d in range(1, K + 1):
            mu[d] = (rho[d] + tau[d]) / Z
    else:
        raise ValueError(f"未知度分布: {dist}")

    cdf = [0.0]
    cum = 0.0
    for d in range(1, K + 1):
        cum += mu[d]
        cdf.append(cum)
    cdf[-1] = 1.0   # 规整化浮点误差
    return cdf


def _sample_degree(rng: random.Random, cdf: list[float]) -> int:
    u = rng.random()
    d = bisect.bisect_left(cdf, u, lo=1, hi=len(cdf))
    return min(d, len(cdf) - 1)


def _sample_adjacency(rng: random.Random, K: int, degree: int) -> tuple[int, ...]:
    """无放回均匀采样 degree 个不同源块索引，返回升序 tuple。"""
    return tuple(sorted(rng.sample(range(K), degree)))


def _xor_blocks(blocks: list[bytes]) -> bytes:
    """对等长块做按字节异或。degree >= 1。"""
    acc = 0
    for b in blocks:
        acc ^= int.from_bytes(b, "little")
    return acc.to_bytes(len(blocks[0]), "little")


def _xor_into(dst: bytearray, src: bytes) -> None:
    """dst ^= src（等长，就地修改）。"""
    for i, b in enumerate(src):
        dst[i] ^= b


# ── 编码器 ───────────────────────────────────────────────
class LtEncoder:
    """对 K 个定长源块做 LT 喷泉编码。"""

    def __init__(self, blocks: list[bytes], session_id: int,
                 dist: int = DIST_RSD, c: float = _RSD_C, delta: float = _RSD_DELTA):
        self.blocks = list(blocks)
        self.K = len(self.blocks)
        if self.K <= 0:
            raise ValueError("至少需要 1 个源块")
        self.block_size = len(self.blocks[0])
        for b in self.blocks:
            if len(b) != self.block_size:
                raise ValueError("所有源块必须等长")
        self.session_id = session_id
        self._dist = dist
        self._cdf = (None if dist == DIST_DEGENERATE
                     else _build_degree_cdf(self.K, dist, c, delta))
        # degree 上限：保证 DATA 帧 (header 18 + degree 2 + adjacency D×4 + xor C + crc 4) ≤ SAFE_FRAME_BYTES
        overhead = 18 + 2 + self.block_size + 4
        if overhead >= SAFE_FRAME_BYTES:
            raise ValueError(
                f"block_size={self.block_size} 过大，XOR_DATA 单独即超 QR 安全容量 "
                f"({overhead}B ≥ {SAFE_FRAME_BYTES}B)，请减小 CHUNK_SIZE_LOG")
        self._degree_cap = max(1, min((SAFE_FRAME_BYTES - overhead) // 4, MAX_DEGREE))

    def encode_symbol(self, symbol_id: int) -> tuple[int, tuple[int, ...], bytes]:
        """确定性生成 symbol_id 对应的编码符号 -> (degree, adjacency, xor_data)。"""
        rng = random.Random(self._seed_for(symbol_id))
        if self._cdf is None:   # DEGENERATE：均匀采样度
            degree = 1 if self.K == 1 else (1 + rng.randint(0, self.K - 1))
        else:
            degree = _sample_degree(rng, self._cdf)
        degree = min(degree, self._degree_cap, self.K)   # 限上限，防 ADJACENCY 溢出 QR 容量
        adjacency = _sample_adjacency(rng, self.K, degree)
        xor_data = _xor_blocks([self.blocks[i] for i in adjacency])
        return degree, adjacency, xor_data

    def _seed_for(self, symbol_id: int) -> int:
        return (int(self.session_id) << 32) | (int(symbol_id) & 0xFFFFFFFF)


# ── 解码器（peeling decoder / 解涟漪）────────────────────
class LtDecoder:
    """累积编码符号，置信传播恢复全部源块。"""

    def __init__(self, num_blocks: int, block_size: int):
        self.K = num_blocks
        self.C = block_size
        self.resolved: list[bytes | None] = [None] * num_blocks
        self._pending_adj: list[list[int]] = []      # 每个待解符号的剩余邻接
        self._pending_data: list[bytearray] = []     # 对应的当前 xor 数据
        self._ripple: deque[int] = deque()           # 新解出、待传播的源块索引

    @property
    def resolved_count(self) -> int:
        return sum(1 for b in self.resolved if b is not None)

    @property
    def is_complete(self) -> bool:
        return self.resolved_count >= self.K

    def add_symbol(self, adjacency: tuple[int, ...] | list[int], xor_data: bytes) -> None:
        """注入一个编码符号（先用已解块消减，再视剩余度决定入队/立即解开）。"""
        data = bytearray(xor_data)
        remaining: list[int] = []
        for i in adjacency:
            if self.resolved[i] is None:
                remaining.append(i)
            else:
                _xor_into(data, self.resolved[i])
        if not remaining:
            return
        if len(remaining) == 1:
            self._resolve(remaining[0], bytes(data))
        else:
            self._pending_adj.append(remaining)
            self._pending_data.append(data)
        self._propagate()

    def _resolve(self, block_i: int, data: bytes) -> None:
        if self.resolved[block_i] is None:
            self.resolved[block_i] = data
            self._ripple.append(block_i)

    def _propagate(self) -> None:
        while self._ripple:
            j = self._ripple.popleft()
            rj = self.resolved[j]
            adj_next: list[list[int]] = []
            data_next: list[bytearray] = []
            for adj, data in zip(self._pending_adj, self._pending_data):
                if j in adj:
                    _xor_into(data, rj)
                    adj.remove(j)
                    if len(adj) == 1:
                        self._resolve(adj[0], bytes(data))
                    elif len(adj) >= 2:
                        adj_next.append(adj)
                        data_next.append(data)
                else:
                    adj_next.append(adj)
                    data_next.append(data)
            self._pending_adj = adj_next
            self._pending_data = data_next

    def get_blocks(self) -> list[bytes]:
        """返回已恢复的源块列表（未完成时含 None）。"""
        return list(self.resolved)   # type: ignore[return-value]
