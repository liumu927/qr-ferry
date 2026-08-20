package com.qrferry.receiver.send

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream

/**
 * FileOptimizer 纯 JVM 路径单测（zip 嗅探 / OOXML 重打包）。
 * Bitmap 相关（PNG→JPEG / 降位）依赖 Android 运行时，由真机验证。
 */
class FileOptimizerTest {

    private fun storedZip(entries: Map<String, ByteArray>): ByteArray {
        val out = ByteArrayOutputStream()
        ZipOutputStream(out).use { z ->
            entries.forEach { (name, payload) ->
                val e = ZipEntry(name).apply {
                    method = ZipEntry.STORED
                    size = payload.size.toLong()
                    compressedSize = payload.size.toLong()
                    val c = CRC32(); c.update(payload); crc = c.value
                }
                z.putNextEntry(e)
                z.write(payload)
                z.closeEntry()
            }
        }
        return out.toByteArray()
    }

    private fun zipNames(data: ByteArray): List<String> {
        val names = mutableListOf<String>()
        ZipInputStream(ByteArrayInputStream(data)).use { z ->
            var e = z.nextEntry
            while (e != null) {
                names.add(e.name)
                z.closeEntry()
                e = z.nextEntry
            }
        }
        return names
    }

    @Test
    fun isOoxmlDetectsContentTypesMarker() {
        val docx = storedZip(mapOf(
            "[Content_Types].xml" to "<Types/>".toByteArray(),
            "word/document.xml" to "<doc/>".toByteArray(),
        ))
        assertTrue(FileOptimizer.isOoxml(docx))
    }

    @Test
    fun isOoxmlRejectsPlainZip() {
        assertFalse(FileOptimizer.isOoxml(storedZip(mapOf("a.txt" to "hello".toByteArray()))))
    }

    @Test
    fun isOoxmlRejectsNonZipAndEmpty() {
        assertFalse(FileOptimizer.isOoxml(byteArrayOf(0, 1, 2, 3)))
        assertFalse(FileOptimizer.isOoxml(ByteArray(0)))
    }

    @Test
    fun optimizeRepacksStoredOoxmlSmaller() {
        val xml = ("<w:document>" + ("<w:p>qr-ferry 光学摆渡</w:p>".repeat(80)) + "</w:document>").toByteArray()
        val data = storedZip(mapOf(
            "[Content_Types].xml" to "<Types/>".toByteArray(),
            "word/document.xml" to xml,
        ))
        val res = FileOptimizer.optimize(data, "报告.docx", lossy = true)
        assertNotNull("STORED 的 docx 重打包应显著变小", res)
        assertTrue(res!!.optimizedSize < res.originalSize)
        assertFalse("无内嵌图片 → 纯重打包，非有损", res.lossy)
        assertTrue(
            zipNames(res.data).containsAll(listOf("[Content_Types].xml", "word/document.xml"))
        )
    }

    @Test
    fun optimizeStripsThumbnail() {
        val data = storedZip(mapOf(
            "[Content_Types].xml" to "<Types/>".toByteArray(),
            "docProps/thumbnail.jpeg" to ByteArray(300) { 1 },
            "word/document.xml" to ("<doc/>" + "x".repeat(5000)).toByteArray(),
        ))
        val res = FileOptimizer.optimize(data, "a.docx")
        assertNotNull(res)
        assertFalse(zipNames(res!!.data).contains("docProps/thumbnail.jpeg"))
        assertEquals(listOf("[Content_Types].xml", "word/document.xml"), zipNames(res.data))
    }

    @Test
    fun optimizeReturnsNullForUnknownOrEmpty() {
        assertNull(FileOptimizer.optimize(ByteArray(0), "x.bin"))
        assertNull(FileOptimizer.optimize(ByteArray(4096) { it.toByte() }, "rand.bin"))
    }
}
