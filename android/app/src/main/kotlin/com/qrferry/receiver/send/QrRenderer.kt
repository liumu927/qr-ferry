package com.qrferry.receiver.send

import android.graphics.Bitmap
import android.graphics.Color
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel
import com.google.zxing.qrcode.encoder.Encoder
import kotlin.math.roundToInt

/** 静区占输出边长的比例。所有 QR 版本统一，白边不随码体版本跳变。 */
internal const val QUIET_RATIO = 0.05

/** 模块缩放倍率与白边像素（码体始终为整数倍缩放，模块边缘无插值灰边）。 */
internal data class QrLayout(val multiple: Int, val marginPx: Int, val outPx: Int) {
    val codePx: Int get() = outPx - 2 * marginPx
}

/**
 * 计算布局：码体按「预算宽度的 90%」取整数倍，白边补齐到输出边长的 [QUIET_RATIO]。
 *
 * 不交给 zxing 按 720 画布取整——那样模块数除不尽的余量会被摊到四周，白边随版本
 * 在 6~57px 之间跳变（MANIFEST 小码窄、DATA 大码宽），同一场发送里肉眼可见。
 */
internal fun qrLayout(modules: Int, budgetPx: Int): QrLayout {
    require(modules > 0) { "modules 必须为正" }
    val marginBudget = (budgetPx * QUIET_RATIO).roundToInt()
    val available = (budgetPx - 2 * marginBudget).coerceAtLeast(modules)
    val multiple = available / modules
    val codePx = modules * multiple
    val marginPx = (codePx * QUIET_RATIO / (1 - 2 * QUIET_RATIO)).roundToInt()
    return QrLayout(multiple, marginPx, codePx + 2 * marginPx)
}

/** 标准 QR 渲染器，强制 ISO-8859-1 逐字节承载协议帧。 */
class QrRenderer(private val sizePx: Int = 720) {
    fun render(
        payload: ByteArray,
        ecLevel: ErrorCorrectionLevel = ErrorCorrectionLevel.M,
    ): Bitmap {
        val text = String(payload, Charsets.ISO_8859_1)
        // Encoder.encode 对空内容不报错（会产出空码），保持与旧 MultiFormatWriter 一致的失败语义
        require(payload.isNotEmpty()) { "payload 不能为空" }
        val hints = mapOf(
            EncodeHintType.CHARACTER_SET to "ISO-8859-1",
            EncodeHintType.ERROR_CORRECTION to ecLevel,
        )
        // 直接取模块矩阵（不缩放、不含静区），布局与静区由本类统一控制
        val matrix = Encoder.encode(text, ecLevel, hints).matrix
        val modules = matrix.width
        val layout = qrLayout(modules, sizePx)

        val pixels = IntArray(layout.outPx * layout.outPx) { Color.WHITE }
        for (my in 0 until modules) {
            val rowTop = (layout.marginPx + my * layout.multiple) * layout.outPx
            for (mx in 0 until modules) {
                if (matrix.get(mx, my).toInt() != 1) continue
                val left = layout.marginPx + mx * layout.multiple
                for (dy in 0 until layout.multiple) {
                    val start = rowTop + dy * layout.outPx + left
                    pixels.fill(Color.BLACK, start, start + layout.multiple)
                }
            }
        }
        val bitmap = Bitmap.createBitmap(layout.outPx, layout.outPx, Bitmap.Config.ARGB_8888)
        bitmap.setPixels(pixels, 0, layout.outPx, 0, 0, layout.outPx, layout.outPx)
        return bitmap
    }
}
