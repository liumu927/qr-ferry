package com.qrferry.receiver.core

/**
 * 帧编解码 —— 协议 v1.0 §4/§5。逐字节对齐 src/qrferry/core/frame.py。
 *
 * 帧 = 18B Header + 变长 Payload + 4B CRC32（小端）。
 * 所有解析/校验失败统一抛 ProtocolError；接收端捕获后丢弃坏帧，绝不崩溃。
 */

// ── 字节序辅助（全程小端）─────────────────────────────────
private fun u8(b: ByteArray, off: Int): Int = b[off].toInt() and 0xFF

private fun u16Le(b: ByteArray, off: Int): Int =
    (b[off].toInt() and 0xFF) or ((b[off + 1].toInt() and 0xFF) shl 8)

private fun u32Le(b: ByteArray, off: Int): Int =
    (b[off].toInt() and 0xFF) or
        ((b[off + 1].toInt() and 0xFF) shl 8) or
        ((b[off + 2].toInt() and 0xFF) shl 16) or
        ((b[off + 3].toInt() and 0xFF) shl 24)

private fun u64Le(b: ByteArray, off: Int): Long {
    val lo = u32Le(b, off).toLong() and 0xFFFFFFFFL
    val hi = u32Le(b, off + 4).toLong() and 0xFFFFFFFFL
    return lo or (hi shl 32)
}

// ── Header (§4) MAGIC(u16) VERSION(u8) TYPE(u8) SESSION(u32) STREAM(u32) SYMBOL(u32) PAYLOAD_LEN(u16) ──
data class FrameHeader(
    val frameType: Int,
    val sessionId: Int,
    val streamId: Int = 0,
    val symbolId: Int = 0,
    val version: Int = Proto.VERSION,
    val payloadLen: Int = 0,
) {
    companion object {
        fun unpack(buf: ByteArray): FrameHeader {
            if (buf.size < Proto.HEADER_SIZE) throw ProtocolError("header 长度不足")
            val magic = u16Le(buf, 0)
            if (magic != Proto.MAGIC) throw ProtocolError("bad MAGIC: 0x${magic.toString(16)}")
            // 接收端不校验 VERSION（对齐 frame.py:84，仅检 MAGIC）。
            return FrameHeader(
                frameType = u8(buf, 3),
                sessionId = u32Le(buf, 4),
                streamId = u32Le(buf, 8),
                symbolId = u32Le(buf, 12),
                version = u8(buf, 2),
                payloadLen = u16Le(buf, 16),
            )
        }
    }
}

// ── MANIFEST Payload (§5.2) ──
data class ManifestPayload(
    val contentType: Int,
    val compression: Int,
    val chunkSizeLog: Int,
    val ltDist: Int,
    val totalChunks: Int,
    val rawSize: Long,
    val encodedSize: Long,
    val rawSha256: ByteArray,
    val filename: String = "",
) {
    companion object {
        // 固定前缀 = <BBBBI>(8) + <QQ>(16) + <B>(1) = 25
        private const val FIXED_PREFIX = 25

        fun unpack(buf: ByteArray): ManifestPayload {
            if (buf.size < FIXED_PREFIX + Proto.SHA256_SIZE)
                throw ProtocolError("manifest payload 过短")
            val nameLen = u8(buf, 24)
            val nameEnd = FIXED_PREFIX + nameLen
            if (buf.size < nameEnd + Proto.SHA256_SIZE)
                throw ProtocolError("manifest filename/sha256 越界")
            // Kotlin String(bytes, charset) 默认 REPLACE action，等价 Python errors="replace"。
            val filename = String(buf, FIXED_PREFIX, nameLen, Charsets.UTF_8)
            val sha = buf.copyOfRange(nameEnd, nameEnd + Proto.SHA256_SIZE)
            return ManifestPayload(
                contentType = u8(buf, 0),
                compression = u8(buf, 1),
                chunkSizeLog = u8(buf, 2),
                ltDist = u8(buf, 3),
                totalChunks = u32Le(buf, 4),
                rawSize = u64Le(buf, 8),
                encodedSize = u64Le(buf, 16),
                rawSha256 = sha,
                filename = filename,
            )
        }
    }
}

// ── DATA Payload (§5.3 LT 编码符号) degree(u16) + degree×adjacency(u32) + xor_data ──
data class DataPayload(
    val degree: Int,
    val adjacency: List<Int>,
    val xorData: ByteArray,
) {
    companion object {
        fun unpack(buf: ByteArray): DataPayload {
            if (buf.size < 2) throw ProtocolError("data payload 过短")
            val degree = u16Le(buf, 0)
            val adjEnd = 2 + degree * 4
            if (buf.size < adjEnd) throw ProtocolError("adjacency 越界")
            val adjacency = if (degree == 0) emptyList()
            else (0 until degree).map { u32Le(buf, 2 + it * 4) }
            return DataPayload(degree, adjacency, buf.copyOfRange(adjEnd, buf.size))
        }
    }
}

// ── END Payload (§5.4) 仅 raw_sha256(32B) ──
data class EndPayload(val rawSha256: ByteArray) {
    companion object {
        fun unpack(buf: ByteArray): EndPayload {
            // 严格 != 32B 即拒（对齐 frame.py:178，多余字节也拒）。
            if (buf.size != Proto.SHA256_SIZE)
                throw ProtocolError("end payload 必须正好 32 字节")
            return EndPayload(buf.copyOfRange(0, Proto.SHA256_SIZE))
        }
    }
}

/**
 * 解析帧并校验 CRC。任何异常抛 ProtocolError，调用方捕获后丢弃。
 * 对齐 frame.py:191-205 decode_frame。
 */
fun decodeFrame(raw: ByteArray): Pair<FrameHeader, ByteArray> {
    if (raw.size < Proto.HEADER_SIZE + Proto.CRC_SIZE)
        throw ProtocolError("帧长度不足最小值")
    val header = FrameHeader.unpack(raw)
    val payloadEnd = Proto.HEADER_SIZE + header.payloadLen
    if (raw.size != payloadEnd + Proto.CRC_SIZE)
        throw ProtocolError("帧长度与 payload_len 不符: got ${raw.size}, expect ${payloadEnd + Proto.CRC_SIZE}")
    val payload = raw.copyOfRange(Proto.HEADER_SIZE, payloadEnd)
    val expected = u32Le(raw, payloadEnd)
    val actual = crc32(raw.copyOfRange(0, payloadEnd))
    if (expected != actual) throw ProtocolError("CRC32 校验失败")
    return header to payload
}
