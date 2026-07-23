package com.qrferry.receiver.core

import com.qrferry.receiver.core.fixture.Fixtures
import com.qrferry.receiver.core.fixture.hexToBytes
import com.qrferry.receiver.core.fixture.toHex
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.assertThrows

class FrameCodecTest {

    private val cases = Fixtures.load().cases

    @Test
    fun `每帧 decodeFrame 成功且 header 字段正确`() {
        for (c in cases) {
            val (mh, _) = decodeFrame(hexToBytes(c.manifestFrameHex))
            assertEquals(FrameType.MANIFEST.value, mh.frameType, "manifest type(${c.name})")
            assertEquals(c.sessionId, mh.sessionId, "session(${c.name})")
            assertEquals(Proto.VERSION, mh.version, "version(${c.name})")
            assertEquals(0, mh.streamId, "stream(${c.name})")

            val (eh, _) = decodeFrame(hexToBytes(c.endFrameHex))
            assertEquals(FrameType.END.value, eh.frameType)

            for (s in c.dataSymbols) {
                val (dh, _) = decodeFrame(hexToBytes(s.frameHex))
                assertEquals(FrameType.DATA.value, dh.frameType)
                assertEquals(c.sessionId, dh.sessionId)
                assertEquals(s.symbolId, dh.symbolId, "symbolId(${c.name})")
            }
        }
    }

    @Test
    fun `MANIFEST payload 字段对照 fixtures`() {
        for (c in cases) {
            val (_, payload) = decodeFrame(hexToBytes(c.manifestFrameHex))
            val m = ManifestPayload.unpack(payload)
            assertEquals(c.contentType, m.contentType, "content_type(${c.name})")
            assertEquals(c.compression, m.compression)
            assertEquals(c.ltDist, m.ltDist)
            assertEquals(c.chunkSizeLog, m.chunkSizeLog)
            assertEquals(c.K, m.totalChunks)
            assertEquals(c.rawSize, m.rawSize)
            assertEquals(c.encodedSize, m.encodedSize)
            assertEquals(c.filename, m.filename)
            assertEquals(c.rawSha256, m.rawSha256.toHex(), "sha256(${c.name})")
        }
    }

    @Test
    fun `DATA payload 字段对照 fixtures`() {
        for (c in cases) {
            val blockSize = 1 shl c.chunkSizeLog
            for (s in c.dataSymbols) {
                val (_, payload) = decodeFrame(hexToBytes(s.frameHex))
                val dp = DataPayload.unpack(payload)
                assertEquals(s.degree, dp.degree, "degree(${c.name},symbol=${s.symbolId})")
                assertEquals(s.adjacency, dp.adjacency, "adjacency(${c.name},symbol=${s.symbolId})")
                assertEquals(blockSize, dp.xorData.size, "xor_data 长度(${c.name},symbol=${s.symbolId})")
                assertEquals(s.xorDataHex, dp.xorData.toHex(), "xor_data(${c.name},symbol=${s.symbolId})")
            }
        }
    }

    @Test
    fun `END payload 严格 32 字节，多或少都拒`() {
        for (c in cases) {
            val (_, payload) = decodeFrame(hexToBytes(c.endFrameHex))
            assertEquals(Proto.SHA256_SIZE, payload.size)
            val ep = EndPayload.unpack(payload)
            assertEquals(c.rawSha256, ep.rawSha256.toHex())
        }
        assertThrows<ProtocolError> { EndPayload.unpack(ByteArray(33)) }
        assertThrows<ProtocolError> { EndPayload.unpack(ByteArray(31)) }
    }

    @Test
    fun `坏帧 - 篡改 payload 字节后 CRC 校验失败`() {
        val raw = hexToBytes(cases[0].manifestFrameHex).copyOf()
        raw[25] = (raw[25].toInt() xor 0xFF).toByte()   // payload 区
        assertThrows<ProtocolError> { decodeFrame(raw) }
    }

    @Test
    fun `坏帧 - 截断抛 ProtocolError`() {
        val raw = hexToBytes(cases[0].manifestFrameHex)
        assertThrows<ProtocolError> { decodeFrame(raw.copyOf(raw.size - 1)) }
    }

    @Test
    fun `坏帧 - MAGIC 不符抛 ProtocolError`() {
        val raw = hexToBytes(cases[0].manifestFrameHex).copyOf()
        raw[0] = 0x00
        assertThrows<ProtocolError> { decodeFrame(raw) }
    }

    @Test
    fun `接收端不校验 VERSION - 非法 version 仍可解码`() {
        // 对齐 frame.py:84：仅检 MAGIC，version 字段任意值都不拒。
        val raw = hexToBytes(cases[0].manifestFrameHex).copyOf()
        raw[2] = 0x7F   // 改 version，重算 CRC
        rewriteCrc(raw)
        val (h, _) = decodeFrame(raw)   // 不应抛错
        assertEquals(0x7F, h.version)
    }

    private fun rewriteCrc(raw: ByteArray) {
        val payloadEnd = raw.size - Proto.CRC_SIZE
        val c = crc32(raw.copyOfRange(0, payloadEnd))
        raw[payloadEnd] = (c and 0xFF).toByte()
        raw[payloadEnd + 1] = ((c shr 8) and 0xFF).toByte()
        raw[payloadEnd + 2] = ((c shr 16) and 0xFF).toByte()
        raw[payloadEnd + 3] = ((c shr 24) and 0xFF).toByte()
    }
}
