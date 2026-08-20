package com.qrferry.receiver.send

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.zip.Deflater
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

/**
 * Android 发送端文件优化 —— 对齐 PC 端 qrferry/app/file_optimizer.py。
 *
 * 通用压缩（ZLIB/ZSTD）对 PNG/DOCX/PDF 等已压缩容器无效；本模块按 magic bytes
 * 分派格式专属再编码（Android 用 Bitmap/java.util.zip，PC 用 Pillow/pikepdf）：
 * - PNG    照片 → JPEG(q85)（有损，数倍收益）；RGB_565 降位重存（有损）；无损重存兜底；
 * - OOXML  zip9 重打包 + 内嵌图片再压缩（有损）+ 剥离缩略图；
 * - PDF    Android 无重压缩库，跳过（透传）。
 *
 * 语义与 PC 一致：只在「结果严格更小」时返回；有损变换改变文件内容/格式
 * （如 .png → .jpg），须由用户在 UI 显式勾选「优化文件」后才会走此路径。
 */
data class OptimizationResult(
    val data: ByteArray,
    val filename: String,
    val originalSize: Int,
    val optimizedSize: Int,
    val lossy: Boolean,
    val description: String,
)

object FileOptimizer {

    private val PNG_MAGIC = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)
    private const val OOXML_MARKER = "[Content_Types].xml"
    private val MEDIA_DIRS = listOf("word/media/", "xl/media/", "ppt/media/")
    private val MEDIA_EXTS = setOf(".png", ".jpg", ".jpeg")
    private const val JPEG_QUALITY = 85
    private const val MEDIA_JPEG_QUALITY = 82

    /** 按内容优化；无可用优化返回 null。image 路径依赖 Bitmap，真机/模拟器验证。 */
    fun optimize(data: ByteArray, filename: String, lossy: Boolean = true): OptimizationResult? {
        if (data.isEmpty()) return null
        return when {
            startsWithMagic(data, PNG_MAGIC) -> optimizePng(data, filename, lossy)
            isOoxml(data) -> optimizeOoxml(data, filename, lossy)
            else -> null
        }
    }

    private fun startsWithMagic(data: ByteArray, magic: ByteArray): Boolean {
        if (data.size < magic.size) return false
        for (i in magic.indices) {
            if (data[i] != magic[i]) return false
        }
        return true
    }

    // ── 格式嗅探（纯 JVM 可测）────────────────────────────
    fun isOoxml(data: ByteArray): Boolean {
        if (data.size < 4) return false
        val pk = data[0] == 0x50.toByte() && data[1] == 0x4B.toByte()
        if (!pk) return false
        return try {
            ZipInputStream(ByteArrayInputStream(data)).use { z ->
                while (true) {
                    val e = z.nextEntry ?: break
                    if (e.name == OOXML_MARKER) return true
                    z.closeEntry()
                }
                false
            }
        } catch (e: Exception) {
            false
        }
    }

    // ── PNG ───────────────────────────────────────────────
    private fun optimizePng(data: ByteArray, filename: String, lossy: Boolean): OptimizationResult? {
        val bitmap = BitmapFactory.decodeByteArray(data, 0, data.size) ?: return null
        val orig = data.size
        var best: OptimizationResult? = null
        try {
            // 候选 1：无损重存（Android PNG 编码器较基础，通常不更小，仅兜底）
            compressIfSmaller(bitmap, Bitmap.CompressFormat.PNG, 100, orig)?.let {
                best = makeResult(it, filename, orig, lossy = false, desc = "PNG 重存")
            }
            if (lossy) {
                // 候选 2：照片 → JPEG（较大且无透明度）
                val bigEnough = orig >= 200_000 || bitmap.width * bitmap.height >= 512 * 512
                if (bigEnough && !bitmap.hasAlpha()) {
                    compressIfSmaller(bitmap, Bitmap.CompressFormat.JPEG, JPEG_QUALITY, orig)?.let {
                        val jpg = filename.substringBeforeLast('.') + ".jpg"
                        best = better(best, makeResult(it, jpg, orig, lossy = true, desc = "照片转 JPEG(q85)"))
                    }
                }
                // 候选 3：RGB_565 降位重存（Android 版"调色板量化"，有损）
                // 闸门：带透明通道的 PNG 不做降位（RGB_565 无 alpha，会静默丢透明）；
                // 超大图 copy 会再分配位图，跳过以避免内存峰值 OOM
                val pixels = bitmap.width.toLong() * bitmap.height
                if (!bitmap.hasAlpha() && pixels <= 20_000_000L) {
                    val rgb565 = bitmap.copy(Bitmap.Config.RGB_565, false)
                    try {
                        compressIfSmaller(rgb565, Bitmap.CompressFormat.PNG, 100, orig)?.let {
                            best = better(best, makeResult(it, filename, orig, lossy = true, desc = "PNG 降位(RGB565)"))
                        }
                    } finally {
                        rgb565.recycle()
                    }
                }
            }
        } finally {
            bitmap.recycle()
        }
        return best
    }

    private fun compressIfSmaller(
        bmp: Bitmap, fmt: Bitmap.CompressFormat, quality: Int, orig: Int
    ): ByteArray? {
        val out = ByteArrayOutputStream()
        return if (bmp.compress(fmt, quality, out) && out.size() < orig) out.toByteArray() else null
    }

    // ── OOXML（docx/xlsx/pptx）────────────────────────────
    private fun optimizeOoxml(data: ByteArray, filename: String, lossy: Boolean): OptimizationResult? {
        val orig = data.size
        val out = ByteArrayOutputStream()
        var mediaReduced = false
        try {
            ZipOutputStream(out).use { zos ->
                zos.setLevel(Deflater.BEST_COMPRESSION)
                ZipInputStream(ByteArrayInputStream(data)).use { zin ->
                    var entry = zin.nextEntry
                    while (entry != null) {
                        val name = entry.name
                        // 剥离缩略图（冗余缓存，无损语义）
                        if (name == "docProps/thumbnail.jpeg" || name == "docProps/thumbnail.png") {
                            zin.closeEntry()
                            entry = zin.nextEntry
                            continue
                        }
                        var payload = zin.readBytes()
                        if (lossy && isMedia(name)) {
                            recompressMedia(payload)?.let {
                                if (it.size < payload.size) {
                                    payload = it
                                    mediaReduced = true
                                }
                            }
                        }
                        zos.putNextEntry(ZipEntry(name).apply { time = entry.time })
                        zos.write(payload)
                        zos.closeEntry()
                        zin.closeEntry()
                        entry = zin.nextEntry
                    }
                }
            }
        } catch (e: Exception) {
            return null
        }
        val bytes = out.toByteArray()
        if (bytes.size >= orig) return null
        val desc = "OOXML 重打包" + if (mediaReduced) " + 压缩内嵌图片" else ""
        return OptimizationResult(bytes, filename, orig, bytes.size, mediaReduced, desc)
    }

    private fun isMedia(name: String): Boolean {
        val lower = name.lowercase()
        return MEDIA_DIRS.any { lower.startsWith(it) } && MEDIA_EXTS.any { lower.endsWith(it) }
    }

    /** 内嵌图片再压缩：JPEG 重编码 q82；PNG 降位重存。返回更小的字节或 null。 */
    private fun recompressMedia(payload: ByteArray): ByteArray? {
        val bmp = BitmapFactory.decodeByteArray(payload, 0, payload.size) ?: return null
        return try {
            val isPng = startsWithMagic(payload, PNG_MAGIC)
            val fmt = if (isPng) Bitmap.CompressFormat.PNG else Bitmap.CompressFormat.JPEG
            val quality = if (isPng) 100 else MEDIA_JPEG_QUALITY
            // PNG 仅在不透明时降位（RGB_565 无 alpha，避免内嵌透明图丢透明）
            val src = if (isPng && !bmp.hasAlpha() && bmp.config != Bitmap.Config.RGB_565)
                bmp.copy(Bitmap.Config.RGB_565, false) else bmp
            try {
                val out = ByteArrayOutputStream()
                val ok = src.compress(fmt, quality, out)
                if (ok && out.size() < payload.size) out.toByteArray() else null
            } finally {
                if (src !== bmp) src.recycle()
            }
        } finally {
            bmp.recycle()
        }
    }

    // ── 公共 ──────────────────────────────────────────────
    private fun makeResult(
        data: ByteArray, filename: String, orig: Int, lossy: Boolean, desc: String
    ): OptimizationResult = OptimizationResult(
        data = data,
        filename = filename,
        originalSize = orig,
        optimizedSize = data.size,
        lossy = lossy,
        description = "$desc: ${fmt(orig)} → ${fmt(data.size)}",
    )

    private fun better(a: OptimizationResult?, b: OptimizationResult?): OptimizationResult? = when {
        a == null -> b
        b == null -> a
        b.optimizedSize < a.optimizedSize -> b
        else -> a
    }

    private fun fmt(n: Int): String = when {
        n >= 1024 * 1024 -> "%.1fMB".format(n / 1024.0 / 1024.0)
        n >= 1024 -> "%.1fKB".format(n / 1024.0)
        else -> "${n}B"
    }
}
