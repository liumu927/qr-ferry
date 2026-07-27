package com.qrferry.receiver.core

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class LtEncoderTest {

    private fun makeBlocks(k: Int, blockSize: Int): List<ByteArray> =
        (0 until k).map { i ->
            ByteArray(blockSize) { j -> ((i * 31 + j * 7 + 1) and 0xFF).toByte() }
        }

    @Test
    fun `前 K 个符号为系统化直发`() {
        val blocks = makeBlocks(50, 64)
        val enc = LtEncoder(blocks, sessionId = 42)
        for (sid in 0 until 50) {
            val sym = enc.encodeSymbol(sid)
            assertEquals(1, sym.degree, "sid=$sid 应为 degree=1")
            assertEquals(listOf(sid), sym.adjacency, "sid=$sid adjacency 应为 [sid]")
            assertTrue(sym.xorData.contentEquals(blocks[sid]), "sid=$sid 应直发源块")
        }
    }

    @Test
    fun `同 sessionId 与 symbolId 输出确定一致`() {
        val blocks = makeBlocks(30, 64)
        val a = LtEncoder(blocks, sessionId = 7)
        val b = LtEncoder(blocks, sessionId = 7)
        val c = LtEncoder(blocks, sessionId = 8)
        var diffAcrossSessions = 0
        for (sid in 30 until 60) {
            val sa = a.encodeSymbol(sid)
            val sb = b.encodeSymbol(sid)
            assertEquals(sa.degree, sb.degree)
            assertEquals(sa.adjacency, sb.adjacency)
            assertTrue(sa.xorData.contentEquals(sb.xorData))
            val sc = c.encodeSymbol(sid)
            // 跨 session 不要求逐 sid 不同（低度符号偶然撞车合法），只需整体显著不同
            if (sa.degree != sc.degree || sa.adjacency != sc.adjacency ||
                !sa.xorData.contentEquals(sc.xorData)
            ) {
                diffAcrossSessions++
            }
        }
        assertTrue(diffAcrossSessions >= 25, "不同 sessionId 的符号流应显著不同（30 个中 $diffAcrossSessions 个不同）")
    }

    @Test
    fun `冗余符号满足协议约束且 XOR 正确`() {
        val k = 100
        val blockSize = 64
        val blocks = makeBlocks(k, blockSize)
        val enc = LtEncoder(blocks, sessionId = 1234)
        for (sid in k until k + 500) {
            val sym = enc.encodeSymbol(sid)
            assertTrue(sym.degree in 1..LtEncoder.MAX_DEGREE, "degree 越界(sid=$sid): ${sym.degree}")
            assertTrue(sym.degree <= k, "degree 不应超过 K(sid=$sid)")
            assertEquals(sym.degree, sym.adjacency.size, "adjacency 长度应等于 degree(sid=$sid)")
            assertEquals(sym.adjacency.sorted().distinct(), sym.adjacency, "adjacency 须升序去重(sid=$sid)")
            assertTrue(sym.adjacency.all { it in 0 until k }, "adjacency 越界(sid=$sid)")
            val expect = ByteArray(blockSize)
            for (i in sym.adjacency) {
                for (j in expect.indices) expect[j] = (expect[j].toInt() xor blocks[i][j].toInt()).toByte()
            }
            assertTrue(expect.contentEquals(sym.xorData), "xorData 不符(sid=$sid)")
        }
    }

    @Test
    fun `端到端 - K=100 前 2K 个符号解码完成且数据一致`() {
        val k = 100
        val blockSize = 64
        val blocks = makeBlocks(k, blockSize)
        val enc = LtEncoder(blocks, sessionId = 99)
        val dec = LtDecoder(k, blockSize)
        for (sid in 0 until 2 * k) {
            val sym = enc.encodeSymbol(sid)
            dec.addSymbol(sym.adjacency, sym.xorData)
        }
        assertTrue(dec.isComplete, "2K 符号内未完成（对齐协议 §12 REDUNDANCY=2.0 兜底口径）")
        val out = dec.getBlocks()
        for (i in 0 until k) {
            assertTrue(blocks[i].contentEquals(out[i]!!), "源块 $i 恢复不符")
        }
    }

    @Test
    fun `随机丢弃三成符号仍可在 2K 内完成`() {
        val k = 100
        val blockSize = 64
        val blocks = makeBlocks(k, blockSize)
        val enc = LtEncoder(blocks, sessionId = 2024)
        val dec = LtDecoder(k, blockSize)
        // 确定性伪随机丢弃（每 10 个丢第 0/3/7 个），模拟漏帧。
        for (sid in 0 until 2 * k) {
            if (sid % 10 == 0 || sid % 10 == 3 || sid % 10 == 7) continue
            val sym = enc.encodeSymbol(sid)
            dec.addSymbol(sym.adjacency, sym.xorData)
        }
        assertTrue(dec.isComplete, "30% 丢符号下 2K 内未完成")
    }
}
