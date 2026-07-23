package com.qrferry.receiver.core

/**
 * 接收会话状态机 —— 协议 v1.0 §7。对齐 src/qrferry/core/session.py: ReceiveSession。
 *
 * MANIFEST 建立上下文（重复幂等），DATA 累积送入 LtDecoder，END 触发收尾。
 * 输出重组后的压缩字节流；解压与 SHA-256 校验交上层（Layer 4）。坏 payload 静默丢弃。
 */
class ReceiveSession {
    var sessionId: Int? = null
        private set
    var manifest: ManifestPayload? = null
        private set
    private var decoder: LtDecoder? = null
    private var ended: Boolean = false

    val started: Boolean get() = manifest != null
    val K: Int get() = manifest?.totalChunks ?: 0
    val isComplete: Boolean get() = decoder?.isComplete ?: false

    /** 已恢复源块占比 [0.0, 1.0]。 */
    val progress: Double
        get() {
            val d = decoder ?: return 0.0
            return if (K == 0) 0.0 else d.resolvedCount.toDouble() / K
        }

    /** 仍未恢复的源块索引；会话未建立时为空。 */
    val missingIndices: List<Int> get() = decoder?.missingIndices ?: emptyList()

    /** 按 FRAME_TYPE 路由；未知会话或坏 payload 一律静默忽略。对齐 session.py:57-67。 */
    fun ingest(header: FrameHeader, payload: ByteArray) {
        try {
            when (header.frameType) {
                FrameType.MANIFEST.value -> onManifest(header, payload)
                FrameType.DATA.value -> onData(header, payload)
                FrameType.END.value -> onEnd(header, payload)
            }
        } catch (e: ProtocolError) {
            return   // 坏 payload：丢弃，不崩溃
        }
    }

    private fun onManifest(header: FrameHeader, payload: ByteArray) {
        val m = ManifestPayload.unpack(payload)
        if (ContentType.of(m.contentType) == null) throw ProtocolError("未知 content_type")
        if (m.compression != Compression.NONE.value && m.compression != Compression.ZLIB.value)
            throw ProtocolError("未知或暂不支持 compression")
        if (LtDistribution.of(m.ltDist) == null) throw ProtocolError("未知 lt_dist")
        if (m.totalChunks <= 0) throw ProtocolError("total_chunks 必须为正")
        if (m.chunkSizeLog !in 1..20) throw ProtocolError("chunk_size_log 越界")
        val blockSize = 1 shl m.chunkSizeLog
        if (m.encodedSize > m.totalChunks.toLong() * blockSize)
            throw ProtocolError("encoded_size 超出分块容量")
        if (started && sessionId == header.sessionId) return   // 同会话重播，幂等忽略
        // 首次或切换会话：重建上下文
        sessionId = header.sessionId
        manifest = m
        decoder = LtDecoder(m.totalChunks, blockSize)
        ended = false
    }

    private fun onData(header: FrameHeader, payload: ByteArray) {
        val d = decoder ?: return
        val m = manifest ?: return
        if (header.sessionId != sessionId) return   // 未知会话 DATA：忽略（MANIFEST 未到）
        val dp = DataPayload.unpack(payload)
        val blockSize = 1 shl m.chunkSizeLog
        if (dp.degree <= 0 || dp.xorData.size != blockSize)
            throw ProtocolError("DATA 符号尺寸非法")
        if (dp.adjacency.sorted().distinct() != dp.adjacency)
            throw ProtocolError("DATA adjacency 必须升序且去重")
        if (dp.adjacency.any { it < 0 || it >= K })
            throw ProtocolError("DATA adjacency 越界")
        d.addSymbol(dp.adjacency, dp.xorData)
    }

    private fun onEnd(header: FrameHeader, payload: ByteArray) {
        if (decoder == null || header.sessionId != sessionId) return
        EndPayload.unpack(payload)   // 仅校验合法性
        ended = true
    }

    /** 返回重组后的压缩字节流（调用前需 is_complete）。对齐 session.py:112 + chunker.join。 */
    fun reassemble(): ByteArray {
        val d = decoder ?: throw IllegalStateException("会话未完成，无法重组")
        val m = manifest ?: throw IllegalStateException("会话未完成，无法重组")
        if (!isComplete) throw IllegalStateException("会话未完成，无法重组")
        val out = ByteArray(d.numBlocks * d.blockSize)
        var pos = 0
        for (b in d.getBlocks()) {
            val block = b ?: throw IllegalStateException("存在未解块")
            System.arraycopy(block, 0, out, pos, block.size)
            pos += block.size
        }
        return out.copyOfRange(0, m.encodedSize.toInt())
    }
}
