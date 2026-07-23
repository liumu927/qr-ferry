package com.qrferry.receiver.core

import java.util.zip.CRC32

/**
 * CRC32（IEEE 802.3），与 zlib.crc32 / Python crc32.calc / ZIP CRC32 完全一致。
 * 直接复用 JDK 实现，勿自造查表轮子。
 */
fun crc32(data: ByteArray): Int {
    val c = CRC32()
    c.update(data)
    return c.value.toInt()
}
