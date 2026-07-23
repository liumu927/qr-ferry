package com.qrferry.receiver.core

/**
 * 协议 v1.0 常量与枚举。逐字节对齐 src/qrferry/core/frame.py。
 */
object Proto {
    /** 帧同步标识，用于过滤摄像头误识别；小端存储为 0x51 0x4F。 */
    const val MAGIC: Int = 0x4F51
    const val VERSION: Int = 0x01

    const val HEADER_SIZE: Int = 18
    const val CRC_SIZE: Int = 4
    const val SHA256_SIZE: Int = 32
    const val MAX_FILENAME: Int = 255
}

/** 帧类型 —— frame.py: FrameType。4/5 为预留项，接收端不处理。 */
enum class FrameType(val value: Int) {
    MANIFEST(0x01),
    DATA(0x02),
    END(0x03),
    FEEDBACK(0x04),
    CIMBAR(0x05);

    companion object {
        fun of(value: Int): FrameType? = entries.firstOrNull { it.value == value }
    }
}

enum class ContentType(val value: Int) {
    FILE(0x01),
    TEXT(0x02);

    companion object {
        fun of(value: Int): ContentType? = entries.firstOrNull { it.value == value }
    }
}

enum class Compression(val value: Int) {
    NONE(0x00),
    ZLIB(0x01),
    ZSTD(0x02);  // 预留

    companion object {
        fun of(value: Int): Compression? = entries.firstOrNull { it.value == value }
    }
}

enum class LtDistribution(val value: Int) {
    RSD(0x00),
    ISD(0x01),
    DEGENERATE(0x02);

    companion object {
        fun of(value: Int): LtDistribution? = entries.firstOrNull { it.value == value }
    }
}
