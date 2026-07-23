package com.qrferry.receiver.core

/**
 * LT 喷泉码 peeling decoder —— 协议 v1.0 §6。对齐 src/qrferry/core/lt.py: LtDecoder。
 *
 * 解码端无需 PRNG / 度分布：degree 与 adjacency 在 DATA 帧显式传输，解码仅做 peeling。
 * 算法：维护 resolved[K]；每个符号先用已解块消减，剩余度 1 即解出入涟漪，
 * 涟漪传播连锁消减 pending 符号。
 */
class LtDecoder(val numBlocks: Int, val blockSize: Int) {

    private val resolved: Array<ByteArray?> = arrayOfNulls(numBlocks)
    private val pendingAdj: MutableList<MutableList<Int>> = mutableListOf()
    private val pendingData: MutableList<ByteArray> = mutableListOf()
    private val ripple: ArrayDeque<Int> = ArrayDeque()

    /** 已恢复源块数。 */
    val resolvedCount: Int get() = resolved.count { it != null }

    /** 仍未恢复的源块索引（升序）。 */
    val missingIndices: List<Int> get() = resolved.indices.filter { resolved[it] == null }

    val isComplete: Boolean get() = resolvedCount >= numBlocks

    /** 注入一个编码符号（先用已解块消减，再视剩余度决定入队/立即解开）。对齐 lt.py:166-182。 */
    fun addSymbol(adjacency: List<Int>, xorData: ByteArray) {
        val data = xorData.copyOf()
        val remaining = mutableListOf<Int>()
        for (i in adjacency) {
            val r = resolved[i]
            if (r == null) remaining.add(i) else xorInto(data, r)
        }
        if (remaining.isEmpty()) return
        if (remaining.size == 1) resolve(remaining[0], data)
        else {
            pendingAdj.add(remaining)
            pendingData.add(data)
        }
        propagate()
    }

    private fun resolve(blockI: Int, data: ByteArray) {
        if (resolved[blockI] == null) {
            resolved[blockI] = data.copyOf()   // 防御性拷贝，对齐 Python bytes(data)
            ripple.addLast(blockI)
        }
    }

    private fun propagate() {
        while (ripple.isNotEmpty()) {
            val j = ripple.removeFirst()
            val rj = resolved[j] ?: continue
            val adjNext = mutableListOf<MutableList<Int>>()
            val dataNext = mutableListOf<ByteArray>()
            for (k in pendingAdj.indices) {
                val adj = pendingAdj[k]
                val data = pendingData[k]
                if (j in adj) {
                    xorInto(data, rj)
                    adj.remove(j)
                    if (adj.size == 1) resolve(adj[0], data)
                    else if (adj.size >= 2) { adjNext.add(adj); dataNext.add(data) }
                } else { adjNext.add(adj); dataNext.add(data) }
            }
            pendingAdj.clear(); pendingAdj.addAll(adjNext)
            pendingData.clear(); pendingData.addAll(dataNext)
        }
    }

    /** 返回已恢复的源块列表（未完成时含 null）。 */
    fun getBlocks(): List<ByteArray?> = resolved.toList()

    /** 等长字节按位异或：dst ^= src（就地修改）。对齐 lt.py:93 _xor_into。 */
    private fun xorInto(dst: ByteArray, src: ByteArray) {
        for (i in src.indices) dst[i] = (dst[i].toInt() xor src[i].toInt()).toByte()
    }
}
