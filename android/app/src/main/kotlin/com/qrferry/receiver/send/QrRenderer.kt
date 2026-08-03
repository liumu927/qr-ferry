package com.qrferry.receiver.send

import android.graphics.Bitmap
import android.graphics.Color
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.MultiFormatWriter
import com.google.zxing.common.BitArray
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel

/** 标准 QR 渲染器，强制 ISO-8859-1 逐字节承载协议帧。 */
class QrRenderer(private val sizePx: Int = 720) {
    fun render(
        payload: ByteArray,
        ecLevel: ErrorCorrectionLevel = ErrorCorrectionLevel.M,
    ): Bitmap {
        val text = String(payload, Charsets.ISO_8859_1)
        val hints = mapOf(
            EncodeHintType.CHARACTER_SET to "ISO-8859-1",
            EncodeHintType.ERROR_CORRECTION to ecLevel,
            EncodeHintType.MARGIN to 2,
        )
        val matrix = MultiFormatWriter().encode(text, BarcodeFormat.QR_CODE, sizePx, sizePx, hints)
        val bitmap = Bitmap.createBitmap(sizePx, sizePx, Bitmap.Config.ARGB_8888)
        // 按行取位、按 32 位字批量填充：全 0/全 1 整字直接 fill，避免 720×720 逐像素单点读写。
        val pixels = IntArray(sizePx * sizePx)
        val rowBits = BitArray(sizePx)
        for (y in 0 until sizePx) {
            val row = matrix.getRow(y, rowBits)
            val rowOffset = y * sizePx
            var x = 0
            for (word in row.bitArray) {
                val runEnd = minOf(sizePx, x + 32)
                when (word) {
                    0 -> pixels.fill(Color.WHITE, rowOffset + x, rowOffset + runEnd)
                    -1 -> pixels.fill(Color.BLACK, rowOffset + x, rowOffset + runEnd)
                    else -> for (xx in x until runEnd) {
                        pixels[rowOffset + xx] = if (row[xx]) Color.BLACK else Color.WHITE
                    }
                }
                x = runEnd
            }
        }
        bitmap.setPixels(pixels, 0, sizePx, 0, 0, sizePx, sizePx)
        return bitmap
    }
}
