package com.qrferry.receiver.pipeline

import com.google.mlkit.vision.barcode.common.Barcode
import com.qrferry.receiver.core.ContentType
import com.qrferry.receiver.core.ManifestPayload
import com.qrferry.receiver.core.Proto
import com.qrferry.receiver.core.ProtocolError
import com.qrferry.receiver.core.ReceiveSession
import com.qrferry.receiver.core.decodeFrame
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import android.os.SystemClock
import java.util.zip.Inflater

/**
 * Layer 4 接收流水线 —— 对齐 src/qrferry/app/receive_pipeline.py。
 * 图像(ML Kit 多码) → decodeFrame → ReceiveSession → 重组解压校验。
 * 坏帧（CRC 失败/MAGIC 不符/解析异常）静默丢弃，绝不崩溃（协议 §11）。
 */
class ReceivePipeline {

    private val session = ReceiveSession()

    val progress: Double get() = session.progress
    val missingIndices: List<Int> get() = session.missingIndices
    val isComplete: Boolean get() = session.isComplete
    val started: Boolean get() = session.started
    val K: Int get() = session.K
    val manifest: ManifestPayload? get() = session.manifest

    // 链路诊断计数
    var seenBarcodes: Int = 0; private set   // ML Kit 返回的码总数
    var validFrames: Int = 0; private set    // 成功 decode + ingest
    var badFrames: Int = 0; private set      // CRC/解析失败被丢弃
    var firstValidFrameAtMs: Long? = null; private set

    val elapsedSeconds: Double
        get() = firstValidFrameAtMs?.let { (SystemClock.elapsedRealtime() - it).coerceAtLeast(0L) / 1000.0 } ?: 0.0

    /** 处理一帧识别到的 QR 码集合；返回是否本次触发完成。 */
    fun process(barcodes: List<Barcode>): Boolean {
        seenBarcodes += barcodes.size
        for (b in barcodes) {
            val raw = b.rawBytes ?: continue
            if (raw.size < Proto.HEADER_SIZE + Proto.CRC_SIZE) continue
            try {
                val (header, payload) = decodeFrame(raw)
                session.ingest(header, payload)
                validFrames++
                if (firstValidFrameAtMs == null) {
                    firstValidFrameAtMs = SystemClock.elapsedRealtime()
                }
                if (session.isComplete) return true
            } catch (e: ProtocolError) {
                badFrames++   // 坏帧丢弃
            }
        }
        return false
    }

    /** 完成后重组 → 解压 → SHA-256 校验，返回结果（TEXT 直接给文本；FILE 给字节+文件名，由上层落盘）。 */
    fun finalize(): Result {
        val m = session.manifest ?: error("会话未建立")
        val encoded = session.reassemble()
        val raw = inflateZlib(encoded)
        val shaHex = MessageDigest.getInstance("SHA-256").digest(raw).toHex()
        val shaOk = shaHex == m.rawSha256.toHex()
        return when (ContentType.of(m.contentType)) {
            ContentType.TEXT -> Result.Text(String(raw, Charsets.UTF_8), shaOk, shaHex)
            ContentType.FILE -> Result.File(
                filename = safeFilename(m.filename.ifBlank { "qrferry.bin" }),
                bytes = raw,
                shaOk = shaOk,
                shaHex = shaHex,
            )
            else -> Result.Text("<未知 content_type=${m.contentType}>", false, shaHex)
        }
    }

    sealed class Result {
        abstract val shaOk: Boolean
        abstract val shaHex: String
        data class Text(val text: String, override val shaOk: Boolean, override val shaHex: String) : Result()
        data class File(val filename: String, val bytes: ByteArray, override val shaOk: Boolean, override val shaHex: String) : Result()
    }

    companion object {
        /** 对齐 receive_pipeline.py: safe_filename —— basename + 过滤非法字符 + 截断 200。 */
        private fun safeFilename(name: String): String {
            val base = File(name).name
            val filtered = base.replace(Regex("[<>:\"/\\\\|?*\\x00-\\x1f]"), "_").trimEnd('.', ' ')
            val cleaned = if (filtered.isBlank()) "qrferry.bin" else filtered
            return if (cleaned.length > 200) cleaned.substring(0, 200) else cleaned
        }

        private fun ByteArray.toHex(): String =
            joinToString("") { (it.toInt() and 0xFF).toString(16).padStart(2, '0') }

        /** 解压标准 zlib 流（Python zlib.compress 产出；Inflater nowrap=false）。 */
        private fun inflateZlib(data: ByteArray): ByteArray {
            val inf = Inflater()
            inf.setInput(data)
            val out = ByteArrayOutputStream()
            val buf = ByteArray(4096)
            while (!inf.finished()) {
                val n = inf.inflate(buf)
                if (n == 0) break
                out.write(buf, 0, n)
            }
            inf.end()
            return out.toByteArray()
        }
    }
}
