package com.qrferry.receiver.core

import com.qrferry.receiver.core.fixture.Fixtures
import com.qrferry.receiver.core.fixture.hexToBytes
import com.qrferry.receiver.core.fixture.inflateZlib
import com.qrferry.receiver.core.fixture.toHex
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.security.MessageDigest

class ReceiveSessionTest {

    private val cases = Fixtures.load().cases

    @Test
    fun `manifest - data - end 顺序 ingest 后完整还原且 SHA-256 通过`() {
        for (c in cases) {
            val sess = ReceiveSession()
            val frames = listOf(c.manifestFrameHex) +
                c.dataSymbols.map { it.frameHex } +
                c.endFrameHex
            for (hex in frames) {
                val (h, p) = decodeFrame(hexToBytes(hex))
                sess.ingest(h, p)
                if (sess.isComplete) break
            }
            assertTrue(sess.isComplete, "未完成(${c.name})")

            val raw = inflateZlib(sess.reassemble())
            assertEquals(c.inputHex, raw.toHex(), "数据不符(${c.name})")

            val sha = MessageDigest.getInstance("SHA-256").digest(raw)
            assertEquals(c.rawSha256, sha.toHex(), "SHA-256 不符(${c.name})")
        }
    }

    @Test
    fun `乱序喂入 DATA 符号也能完成`() {
        val c = cases.first { it.name == "file_binary" }
        val sess = ReceiveSession()
        val (mh, mp) = decodeFrame(hexToBytes(c.manifestFrameHex))
        sess.ingest(mh, mp)
        c.dataSymbols.shuffled().forEach { s ->
            val (h, p) = decodeFrame(hexToBytes(s.frameHex))
            sess.ingest(h, p)
        }
        assertTrue(sess.isComplete, "乱序后未完成")
    }

    @Test
    fun `重复符号幂等 - 二次喂入不影响结果`() {
        val c = cases.first { it.name == "text_short" }
        val sess = ReceiveSession()
        val (mh, mp) = decodeFrame(hexToBytes(c.manifestFrameHex))
        sess.ingest(mh, mp)
        for (s in c.dataSymbols) {
            val (h, p) = decodeFrame(hexToBytes(s.frameHex))
            sess.ingest(h, p)
            sess.ingest(h, p)   // 重复喂入
        }
        assertTrue(sess.isComplete)
        val raw = inflateZlib(sess.reassemble())
        assertEquals(c.inputHex, raw.toHex())
    }

    @Test
    fun `MANIFEST 重复幂等 - 同会话重播不重置上下文`() {
        val c = cases.first { it.name == "text_short" }
        val sess = ReceiveSession()
        val (mh, mp) = decodeFrame(hexToBytes(c.manifestFrameHex))
        sess.ingest(mh, mp)
        val kBefore = sess.K

        val first = c.dataSymbols.first()
        val (dh, dp) = decodeFrame(hexToBytes(first.frameHex))
        sess.ingest(dh, dp)

        sess.ingest(mh, mp)   // 同会话重播 manifest
        assertEquals(kBefore, sess.K, "MANIFEST 重播不应重置")
    }

    @Test
    fun `坏 payload 静默丢弃不崩溃`() {
        val c = cases[0]
        val sess = ReceiveSession()
        val (mh, mp) = decodeFrame(hexToBytes(c.manifestFrameHex))
        sess.ingest(mh, mp)

        // 未知会话的 END（session 不符）→ 静默忽略，不抛、不崩溃
        val badHeader = FrameHeader(FrameType.END.value, sessionId = 999999)
        sess.ingest(badHeader, ByteArray(Proto.SHA256_SIZE))
        assertFalse(sess.isComplete)
    }
}
