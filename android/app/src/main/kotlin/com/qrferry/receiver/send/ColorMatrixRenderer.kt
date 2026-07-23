package com.qrferry.receiver.send

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import com.google.zxing.common.reedsolomon.GenericGF
import com.google.zxing.common.reedsolomon.ReedSolomonEncoder
import java.util.zip.CRC32

/**
 * 4 色矩阵码渲染器。对齐 Python ColorMatrixBackend：
 * 固定调色板标定区 + MAGIC/长度/CRC/负载 + 分块 Reed-Solomon 纠错。
 */
class ColorMatrixRenderer(
    private val quietModules: Int = 2,
    private val borderModules: Int = 2,
    private val cellGapPx: Int = 1,
) {
    val maxPayload: Int = layoutForGrid(MAX_GRID).dataCapacity - HEADER_SIZE
    private val rsEncoder = ReedSolomonEncoder(GenericGF.QR_CODE_FIELD_256)

    private val palette = intArrayOf(
        Color.rgb(248, 113, 113),
        Color.rgb(34, 197, 94),
        Color.rgb(96, 165, 250),
        Color.rgb(250, 204, 21),
    )

    fun render(payload: ByteArray): Bitmap {
        val layout = selectLayout(payload.size)
        val packet = ByteArray(layout.dataCapacity) { balancedPadding(it) }
        MAGIC.copyInto(packet, 0)
        packet[4] = ((payload.size ushr 8) and 0xFF).toByte()
        packet[5] = (payload.size and 0xFF).toByte()
        val crc = CRC32().also { it.update(payload) }.value
        packet[6] = ((crc ushr 24) and 0xFF).toByte()
        packet[7] = ((crc ushr 16) and 0xFF).toByte()
        packet[8] = ((crc ushr 8) and 0xFF).toByte()
        packet[9] = (crc and 0xFF).toByte()
        payload.copyInto(packet, HEADER_SIZE)
        val encoded = encodeCodewords(packet, layout)

        val size = layout.imageSize
        val bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bitmap)
        val paint = Paint().apply { style = Paint.Style.FILL; isAntiAlias = false }
        paint.color = Color.WHITE
        canvas.drawRect(0f, 0f, size.toFloat(), size.toFloat(), paint)
        fillModules(canvas, paint, layout, quietModules, quietModules, layout.gridSize + 2 * borderModules, Color.BLACK)
        fillModules(canvas, paint, layout, layout.dataOffset, layout.dataOffset, layout.gridSize, Color.WHITE)

        var cell = 0
        for (symbol in CALIBRATION_SYMBOLS) {
            val x = cell % layout.gridSize
            val y = cell / layout.gridSize
            fillCell(canvas, paint, layout, layout.dataOffset + x, layout.dataOffset + y, palette[symbol])
            cell++
        }
        for (byte in encoded) {
            val v = byte.toInt() and 0xFF
            for (shift in intArrayOf(6, 4, 2, 0)) {
                val symbol = (v ushr shift) and 0x03
                val x = cell % layout.gridSize
                val y = cell / layout.gridSize
                fillCell(canvas, paint, layout, layout.dataOffset + x, layout.dataOffset + y, palette[symbol])
                cell++
            }
        }
        while (cell < layout.gridSize * layout.gridSize) {
            val x = cell % layout.gridSize
            val y = cell / layout.gridSize
            fillCell(canvas, paint, layout, layout.dataOffset + x, layout.dataOffset + y, palette[cell and 0x03])
            cell++
        }
        return bitmap
    }

    private fun selectLayout(payloadSize: Int): Layout {
        for (grid in GRID_SIZES) {
            val layout = layoutForGrid(grid)
            if (payloadSize <= layout.dataCapacity - HEADER_SIZE) return layout
        }
        error("payload 超出彩色矩阵容量")
    }

    private fun layoutForGrid(gridSize: Int): Layout {
        val modulePx = when (gridSize) {
            32 -> 18
            48 -> 14
            else -> 12
        }
        return Layout(
            gridSize = gridSize,
            modulePx = modulePx,
            dataOffset = quietModules + borderModules,
        )
    }

    private fun balancedPadding(index: Int): Byte {
        val a = index and 0x03
        val b = (index + 1) and 0x03
        val c = (index + 2) and 0x03
        val d = (index + 3) and 0x03
        return ((a shl 6) or (b shl 4) or (c shl 2) or d).toByte()
    }

    private fun encodeCodewords(packet: ByteArray, layout: Layout): ByteArray {
        val out = ArrayList<Byte>(layout.codewordSizes.sum())
        var offset = 0
        for (codewordSize in layout.codewordSizes) {
            val dataSize = codewordSize - ECC_BYTES
            val codeword = IntArray(codewordSize)
            for (i in 0 until dataSize) {
                codeword[i] = packet[offset + i].toInt() and 0xFF
            }
            rsEncoder.encode(codeword, ECC_BYTES)
            for (value in codeword) out.add(value.toByte())
            offset += dataSize
        }
        return out.toByteArray()
    }

    private fun fillModules(canvas: Canvas, paint: Paint, layout: Layout, moduleX: Int, moduleY: Int, modules: Int, color: Int) {
        val x0 = moduleX * layout.modulePx
        val y0 = moduleY * layout.modulePx
        val pixels = modules * layout.modulePx
        paint.color = color
        canvas.drawRect(x0.toFloat(), y0.toFloat(), (x0 + pixels).toFloat(), (y0 + pixels).toFloat(), paint)
    }

    private fun fillCell(canvas: Canvas, paint: Paint, layout: Layout, moduleX: Int, moduleY: Int, color: Int) {
        val x0 = moduleX * layout.modulePx + cellGapPx
        val y0 = moduleY * layout.modulePx + cellGapPx
        val x1 = (moduleX + 1) * layout.modulePx - cellGapPx
        val y1 = (moduleY + 1) * layout.modulePx - cellGapPx
        paint.color = color
        canvas.drawRect(x0.toFloat(), y0.toFloat(), x1.toFloat(), y1.toFloat(), paint)
    }

    companion object {
        private val MAGIC = byteArrayOf(0x51, 0x46, 0x43, 0x31) // QFC1
        private const val HEADER_SIZE = 10
        private const val ECC_BYTES = 64
        private val CALIBRATION_SYMBOLS = intArrayOf(
            0, 1, 2, 3, 0, 1, 2, 3,
            0, 1, 2, 3, 0, 1, 2, 3,
        )
        private val GRID_SIZES = intArrayOf(32, 48, 64)
        private const val MAX_GRID = 64
    }

    private data class Layout(
        val gridSize: Int,
        val modulePx: Int,
        val dataOffset: Int,
    ) {
        val capacity: Int get() = gridSize * gridSize / 4
        val codewordSizes: IntArray get() = when (gridSize) {
            32 -> intArrayOf(252)
            48 -> intArrayOf(255, 255)
            else -> intArrayOf(255, 255, 255, 255)
        }
        val dataCapacity: Int get() = codewordSizes.sumOf { it - ECC_BYTES }
        val totalModules: Int get() = gridSize + 2 * dataOffset
        val imageSize: Int get() = totalModules * modulePx
    }
}
