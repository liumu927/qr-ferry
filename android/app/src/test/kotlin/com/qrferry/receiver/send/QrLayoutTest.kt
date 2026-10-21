package com.qrferry.receiver.send

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/**
 * QR 布局单测：静区比例必须与 QR 版本无关（纯 JVM，Bitmap 路径由真机验证）。
 * 回归背景：旧实现交给 zxing 在 720 画布上取整，白边随版本在 6~57px 跳变，
 * MANIFEST 小码白边窄、DATA 大码白边宽，同一场发送中肉眼可见。
 */
class QrLayoutTest {

    /** v1..v40 的模块数（17 + 4×version）。 */
    private val allModuleCounts = (1..40).map { 17 + 4 * it }

    @Test
    fun `静区占比在所有版本上一致`() {
        for (modules in allModuleCounts) {
            val l = qrLayout(modules, 720)
            val ratio = l.marginPx.toDouble() / l.outPx
            assertTrue(
                "modules=$modules 静区占比 ${ratio} 偏离 $QUIET_RATIO",
                abs(ratio - QUIET_RATIO) < 0.005,
            )
        }
    }

    @Test
    fun `码体为整数倍缩放且填满输出`() {
        for (modules in allModuleCounts) {
            val l = qrLayout(modules, 720)
            assertEquals("modules=$modules 码体宽必须是模块数整数倍", 0, l.codePx % modules)
            assertEquals("modules=$modules 输出宽必须等于码体+两侧白边", l.outPx, l.codePx + 2 * l.marginPx)
            assertTrue("modules=$modules 倍率至少 1px", l.multiple >= 1)
        }
    }

    @Test
    fun `输出不超过尺寸预算`() {
        for (modules in allModuleCounts) {
            val l = qrLayout(modules, 720)
            assertTrue("modules=$modules 输出 ${l.outPx} 超出预算 720", l.outPx <= 720)
        }
    }

    @Test
    fun `实际发送帧的屏上白边一致`() {
        // 默认 2200B 帧长下：MANIFEST 138B → v7；DATA 2076B → v34。
        // 位图尺寸随版本不同（FIT_CENTER 会等比缩放到同一屏宽），
        // 因此比的是「白边占位图边长的比例」换算到屏幕上的像素。
        val screenWidth = 1220
        val manifest = qrLayout(17 + 4 * 7, 720)
        val data = qrLayout(17 + 4 * 34, 720)
        val manifestOnScreen = manifest.marginPx.toDouble() / manifest.outPx * screenWidth
        val dataOnScreen = data.marginPx.toDouble() / data.outPx * screenWidth
        assertTrue(
            "首帧白边 ${manifestOnScreen}px 与数据帧 ${dataOnScreen}px 相差超过 1px",
            abs(manifestOnScreen - dataOnScreen) <= 1.0,
        )
    }
}
