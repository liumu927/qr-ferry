package com.qrferry.receiver.core

import com.qrferry.receiver.core.fixture.Fixtures
import com.qrferry.receiver.core.fixture.hexToBytes
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class Crc32GoldenTest {

    @Test
    fun `黄金向量 - 123456789 的 CRC32 为 0xCBF43926`() {
        // 标准 CRC32（IEEE 802.3）黄金向量，验证 java.util.zip.CRC32 实现。
        assertEquals(0xCBF43926L.toInt(), crc32("123456789".toByteArray()))
    }

    @Test
    fun `fixtures 每帧尾 CRC 与重算一致`() {
        for (c in Fixtures.load().cases) {
            assertFrameCrc(c.manifestFrameHex, "manifest(${c.name})")
            assertFrameCrc(c.endFrameHex, "end(${c.name})")
            for (s in c.dataSymbols) {
                assertFrameCrc(s.frameHex, "data(${c.name},symbol=${s.symbolId})")
            }
        }
    }

    private fun assertFrameCrc(hex: String, label: String) {
        val raw = hexToBytes(hex)
        val payloadEnd = raw.size - Proto.CRC_SIZE
        val tail = readU32Le(raw, payloadEnd)
        val recomputed = crc32(raw.copyOfRange(0, payloadEnd))
        assertEquals(tail, recomputed, "CRC 不一致: $label")
    }

    private fun readU32Le(b: ByteArray, off: Int): Int =
        (b[off].toInt() and 0xFF) or
            ((b[off + 1].toInt() and 0xFF) shl 8) or
            ((b[off + 2].toInt() and 0xFF) shl 16) or
            ((b[off + 3].toInt() and 0xFF) shl 24)
}
