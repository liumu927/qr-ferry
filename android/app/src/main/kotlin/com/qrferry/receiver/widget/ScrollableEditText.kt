package com.qrferry.receiver.widget

import android.content.Context
import android.util.AttributeSet
import android.view.MotionEvent
import androidx.appcompat.widget.AppCompatEditText

/**
 * 可滚动的多行输入框。
 *
 * 外层控制面板是 ScrollView：纵向拖动默认被父级拦截，导致文本框自身无法滚动。
 * 这里在按下时根据内容是否溢出决定是否用 requestDisallowInterceptTouchEvent 接管手势：
 * - 内容可滚动（有溢出）→ 阻止父级拦截，文本框自己滚动；
 * - 内容未溢出 → 不接管，拖动仍由外层 ScrollView 滚动整个面板。
 */
class ScrollableEditText @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = android.R.attr.editTextStyle,
) : AppCompatEditText(context, attrs, defStyleAttr) {

    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                if (canScrollVertically(1) || canScrollVertically(-1)) {
                    parent?.requestDisallowInterceptTouchEvent(true)
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL ->
                parent?.requestDisallowInterceptTouchEvent(false)
        }
        return super.onTouchEvent(event)
    }
}
