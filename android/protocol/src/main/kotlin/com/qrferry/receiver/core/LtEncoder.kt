package com.qrferry.receiver.core

import kotlin.math.ln
import kotlin.math.min
import kotlin.math.sqrt

/**
 * LT 喷泉码编码器 —— 协议 v1.0 §6。语义对齐 src/qrferry/core/lt.py: LtEncoder。
 *
 * 系统化发送：前 K 个符号 degree=1 直发源块（无丢包时一轮即完成）；之后无限生成
 * RSD 随机组合符号，接收端收齐略多于 K 个"任意"符号即可恢复 —— 漏帧无需等下一轮
 * 循环到特定块，消除顺序轮询的补帧长尾。
 *
 * 确定性：同一 (sessionId, symbolId) 恒等映射到同一 (degree, adjacency, xorData)。
 * degree/adjacency 在 DATA 帧中显式传输，解码端无需 PRNG；PRNG 无需与 Python 端一致，
 * 这里固定用 SplitMix64 保证 Android 端自身可复现（测试与续传复放）。
 */
class LtEncoder(
    private val blocks: List<ByteArray>,
    private val sessionId: Int,
    maxFrameBytes: Int = DEFAULT_MAX_FRAME_BYTES,
) {
    /** 编码符号：(degree, adjacency, xorData)，直接对应 DataPayload 三要素。 */
    data class EncodedSymbol(val degree: Int, val adjacency: List<Int>, val xorData: ByteArray)

    val K: Int = blocks.size
    private val blockSize: Int
    private val cdf: DoubleArray
    private val degreeCap: Int

    init {
        require(blocks.isNotEmpty()) { "至少需要 1 个源块" }
        blockSize = blocks[0].size
        require(blocks.all { it.size == blockSize }) { "所有源块必须等长" }
        cdf = buildRsdCdf(K)
        // degree 上限：保证 DATA 帧 (header 18 + degree 2 + adjacency D×4 + xor C + crc 4) ≤ maxFrameBytes
        val overhead = 18 + 2 + blockSize + 4
        require(overhead < maxFrameBytes) {
            "blockSize=$blockSize 过大，XOR_DATA 单独即超 QR 安全容量 (${overhead}B ≥ ${maxFrameBytes}B)"
        }
        degreeCap = maxOf(1, minOf((maxFrameBytes - overhead) / 4, MAX_DEGREE))
    }

    /** 确定性生成 symbolId 对应的编码符号。对齐 lt.py:126-139。 */
    fun encodeSymbol(symbolId: Int): EncodedSymbol {
        require(symbolId >= 0) { "symbolId 必须非负" }
        if (symbolId < K) {
            return EncodedSymbol(1, listOf(symbolId), blocks[symbolId].copyOf())
        }
        val rng = SplitMix64(seedFor(symbolId))
        var degree = sampleDegree(rng)
        degree = minOf(degree, degreeCap, K)   // 限上限，防 ADJACENCY 溢出 QR 容量
        val adjacency = sampleAdjacency(rng, degree)
        val xorData = ByteArray(blockSize)
        for (i in adjacency) {
            val b = blocks[i]
            for (j in xorData.indices) xorData[j] = (xorData[j].toInt() xor b[j].toInt()).toByte()
        }
        return EncodedSymbol(degree, adjacency, xorData)
    }

    private fun seedFor(symbolId: Int): Long =
        ((sessionId.toLong() and 0xFFFFFFFFL) shl 32) or (symbolId.toLong() and 0xFFFFFFFFL)

    /** CDF 二分采样度：对齐 lt.py:74-77 的 bisect_left(cdf, u, lo=1)。 */
    private fun sampleDegree(rng: SplitMix64): Int {
        val u = rng.nextDouble()
        var lo = 1
        var hi = K + 1   // hi 为开边界，等价 Python hi=len(cdf)=K+1
        while (lo < hi) {
            val mid = (lo + hi) ushr 1
            if (cdf[mid] < u) lo = mid + 1 else hi = mid
        }
        return min(lo, K)
    }

    /** 无放回均匀采样 degree 个不同源块索引（部分 Fisher-Yates），返回升序 List。 */
    private fun sampleAdjacency(rng: SplitMix64, degree: Int): List<Int> {
        val idx = IntArray(K) { it }
        for (i in 0 until degree) {
            val j = i + rng.nextInt(K - i)
            val tmp = idx[i]; idx[i] = idx[j]; idx[j] = tmp
        }
        return idx.copyOfRange(0, degree).sorted()
    }

    companion object {
        /** 对齐 lt.py:34 —— 超过此度 ADJACENCY 过长，QR 模块过密、识别率下降。 */
        const val MAX_DEGREE = 30

        /**
         * 单 DATA 帧字节安全上限（Android）。blockSize=512 时 overhead=536，
         * degree≤30 → 帧 ≤656B，QR-M 版本仅比 540B 系统化帧高约 2 档，720px 下模块仍足够大。
         */
        const val DEFAULT_MAX_FRAME_BYTES = 700

        private const val RSD_C = 0.1
        private const val RSD_DELTA = 0.5

        /** 构建 RSD 度分布 CDF，cdf[d]=P(degree<=d)，长度 K+1。逐行对齐 lt.py:41-71。 */
        fun buildRsdCdf(K: Int, c: Double = RSD_C, delta: Double = RSD_DELTA): DoubleArray {
            if (K <= 0) return doubleArrayOf(0.0)
            val rho = DoubleArray(K + 1)
            rho[1] = 1.0 / K
            for (d in 2..K) rho[d] = 1.0 / (d.toDouble() * (d - 1))

            val r = c * ln(K / delta) * sqrt(K.toDouble())
            val tau = DoubleArray(K + 1)
            for (d in 1 until K) tau[d] = r / (d.toDouble() * K)
            tau[K] = if (r / delta > 1) r * ln(r / delta) / K else 0.0
            val z = (1..K).sumOf { rho[it] + tau[it] }

            val cdf = DoubleArray(K + 1)
            var cum = 0.0
            for (d in 1..K) {
                cum += (rho[d] + tau[d]) / z
                cdf[d] = cum
            }
            cdf[K] = 1.0   // 规整化浮点误差
            return cdf
        }
    }
}

/** 固定算法的 SplitMix64 PRNG：确定性、跨 Kotlin 版本稳定（不依赖 kotlin.random 实现细节）。 */
private class SplitMix64(seed: Long) {
    private var state = seed

    fun nextLong(): Long {
        var z = (state + GOLDEN_GAMMA).also { state = it }
        z = (z xor (z ushr 30)) * 0xBF58476D1CE4E5B9uL.toLong()
        z = (z xor (z ushr 27)) * 0x94D049BB133111EBuL.toLong()
        return z xor (z ushr 31)
    }

    /** [0,1) 均匀 double（53 位精度）：(nextLong ushr 11) / 2^53。 */
    fun nextDouble(): Double = (nextLong() ushr 11).toDouble() / 9007199254740992.0

    /** [0,bound) 均匀 int，拒绝采样消除模偏差。 */
    fun nextInt(bound: Int): Int {
        require(bound > 0)
        while (true) {
            val bits = nextLong() ushr 1
            val v = bits % bound
            if (bits - v + (bound - 1) >= 0) return v.toInt()
        }
    }

    companion object {
        private val GOLDEN_GAMMA = 0x9E3779B97F4A7C15uL.toLong()
    }
}
