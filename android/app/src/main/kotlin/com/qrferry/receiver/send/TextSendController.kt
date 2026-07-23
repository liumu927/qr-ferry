package com.qrferry.receiver.send

import com.qrferry.receiver.core.Compression
import com.qrferry.receiver.core.ContentType
import com.qrferry.receiver.core.DataPayload
import com.qrferry.receiver.core.FrameHeader
import com.qrferry.receiver.core.FrameType
import com.qrferry.receiver.core.LtDistribution
import com.qrferry.receiver.core.ManifestPayload
import com.qrferry.receiver.core.encodeFrame
import com.qrferry.receiver.core.pack
import java.security.MessageDigest
import java.util.zip.Deflater

/**
 * Android 发送端：自适应压缩 -> 分块 -> MANIFEST/DATA 顺序滚动帧流。
 */
class TextSendController private constructor(
    private val raw: ByteArray,
    private val contentType: Int,
    private val filename: String,
    val sessionId: Int,
    private val chunkSizeLog: Int,
    private val manifestInterval: Int = DEFAULT_MANIFEST_INTERVAL,
) {
    private val compressed = deflate(raw)
    private val compression = if (compressed.size < raw.size) Compression.ZLIB else Compression.NONE
    private val encoded = if (compression == Compression.ZLIB) compressed else raw
    private val blockSize = 1 shl chunkSizeLog
    private val blocks = splitBlocks(encoded, blockSize)
    private val sha = MessageDigest.getInstance("SHA-256").digest(raw)
    private val manifestFrame = encodeFrame(
        FrameHeader(FrameType.MANIFEST.value, sessionId = sessionId),
        ManifestPayload(
            contentType = contentType,
            compression = compression.value,
            chunkSizeLog = chunkSizeLog,
            ltDist = LtDistribution.RSD.value,
            totalChunks = blocks.size,
            rawSize = raw.size.toLong(),
            encodedSize = encoded.size.toLong(),
            rawSha256 = sha,
            filename = filename,
        ).pack()
    )
    private var dataSinceManifest = manifestInterval
    private var nextSid = 0

    val K: Int get() = blocks.size
    val rawSize: Int get() = raw.size
    val encodedSize: Int get() = encoded.size
    val nextSymbolId: Int get() = nextSid

    constructor(
        text: String,
        sessionId: Int,
        chunkSizeLog: Int,
        manifestInterval: Int = DEFAULT_MANIFEST_INTERVAL,
    ) : this(
        raw = text.toByteArray(Charsets.UTF_8),
        contentType = ContentType.TEXT.value,
        filename = "",
        sessionId = sessionId,
        chunkSizeLog = chunkSizeLog,
        manifestInterval = manifestInterval,
    )

    fun nextFrame(): ByteArray {
        if (dataSinceManifest >= manifestInterval) {
            dataSinceManifest = 0
            return manifestFrame
        }
        dataSinceManifest++
        return nextDataFrame()
    }

    private fun nextDataFrame(): ByteArray {
        val sid = nextSid++
        val blockIndex = sid % blocks.size
        val payload = DataPayload(
            degree = 1,
            adjacency = listOf(blockIndex),
            xorData = blocks[blockIndex],
        ).pack()
        return encodeFrame(
            FrameHeader(FrameType.DATA.value, sessionId = sessionId, symbolId = sid),
            payload
        )
    }

    companion object {
        const val QR_CHUNK_SIZE_LOG = 9
        const val COLOR_CHUNK_SIZE_LOG = 9
        const val DEFAULT_MANIFEST_INTERVAL = 32

        fun forFile(
            filename: String,
            bytes: ByteArray,
            sessionId: Int,
            chunkSizeLog: Int,
            manifestInterval: Int = DEFAULT_MANIFEST_INTERVAL,
        ): TextSendController = TextSendController(
            raw = bytes,
            contentType = ContentType.FILE.value,
            filename = filename,
            sessionId = sessionId,
            chunkSizeLog = chunkSizeLog,
            manifestInterval = manifestInterval,
        )

        private fun splitBlocks(data: ByteArray, blockSize: Int): List<ByteArray> {
            require(data.isNotEmpty()) { "发送数据不能为空" }
            val out = mutableListOf<ByteArray>()
            var offset = 0
            while (offset < data.size) {
                val block = ByteArray(blockSize)
                val n = minOf(blockSize, data.size - offset)
                System.arraycopy(data, offset, block, 0, n)
                out.add(block)
                offset += n
            }
            return out
        }

        private fun deflate(data: ByteArray): ByteArray {
            val deflater = Deflater(Deflater.BEST_COMPRESSION)
            deflater.setInput(data)
            deflater.finish()
            val buf = ByteArray(4096)
            val out = ArrayList<Byte>()
            while (!deflater.finished()) {
                val n = deflater.deflate(buf)
                for (i in 0 until n) out.add(buf[i])
            }
            deflater.end()
            return out.toByteArray()
        }
    }
}
