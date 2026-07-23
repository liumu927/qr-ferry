package com.qrferry.receiver.core

import com.qrferry.receiver.core.fixture.Fixtures
import com.qrferry.receiver.core.fixture.hexToBytes
import com.qrferry.receiver.core.fixture.inflateZlib
import com.qrferry.receiver.core.fixture.toHex
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class LtDecoderTest {

    private val cases = Fixtures.load().cases

    @Test
    fun `全部符号喂入后 isComplete 且解压 == input`() {
        for (c in cases) {
            val blockSize = 1 shl c.chunkSizeLog
            val dec = LtDecoder(c.K, blockSize)
            for (s in c.dataSymbols) {
                dec.addSymbol(s.adjacency, hexToBytes(s.xorDataHex))
            }
            assertTrue(dec.isComplete, "未完成(${c.name})")

            val out = ByteArray(c.K * blockSize)
            var pos = 0
            for (b in dec.getBlocks()) {
                val blk = b!!
                System.arraycopy(blk, 0, out, pos, blk.size)
                pos += blk.size
            }
            val encoded = out.copyOfRange(0, c.encodedSize.toInt())
            val raw = inflateZlib(encoded)
            assertEquals(c.inputHex, raw.toHex(), "解压数据不符(${c.name})")
        }
    }

    @Test
    fun `乱序喂入符号也能完成`() {
        // 喷泉码无序容错：乱序喂入同样恢复全部源块。
        val c = cases.first { it.name == "file_binary" }
        val blockSize = 1 shl c.chunkSizeLog
        val dec = LtDecoder(c.K, blockSize)
        c.dataSymbols.shuffled().forEach { dec.addSymbol(it.adjacency, hexToBytes(it.xorDataHex)) }
        assertTrue(dec.isComplete, "乱序后未完成")
    }

    @Test
    fun `重复符号幂等 - 二次喂入不破坏状态`() {
        val c = cases.first { it.name == "text_short" }
        val blockSize = 1 shl c.chunkSizeLog
        val dec = LtDecoder(c.K, blockSize)
        for (s in c.dataSymbols) {
            dec.addSymbol(s.adjacency, hexToBytes(s.xorDataHex))
            dec.addSymbol(s.adjacency, hexToBytes(s.xorDataHex))   // 重复
        }
        assertTrue(dec.isComplete)
    }
}
