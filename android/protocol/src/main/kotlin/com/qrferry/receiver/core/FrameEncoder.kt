package com.qrferry.receiver.core

import java.io.ByteArrayOutputStream

private fun ByteArrayOutputStream.u8(v: Int) {
    write(v and 0xFF)
}

private fun ByteArrayOutputStream.u16Le(v: Int) {
    write(v and 0xFF)
    write((v ushr 8) and 0xFF)
}

private fun ByteArrayOutputStream.u32Le(v: Int) {
    write(v and 0xFF)
    write((v ushr 8) and 0xFF)
    write((v ushr 16) and 0xFF)
    write((v ushr 24) and 0xFF)
}

private fun ByteArrayOutputStream.u64Le(v: Long) {
    u32Le((v and 0xFFFFFFFFL).toInt())
    u32Le(((v ushr 32) and 0xFFFFFFFFL).toInt())
}

fun ManifestPayload.pack(): ByteArray {
    val name = filename.toByteArray(Charsets.UTF_8)
    if (name.size > Proto.MAX_FILENAME) throw ProtocolError("filename 过长")
    if (rawSha256.size != Proto.SHA256_SIZE) throw ProtocolError("raw_sha256 必须 32 字节")
    return ByteArrayOutputStream().apply {
        u8(contentType)
        u8(compression)
        u8(chunkSizeLog)
        u8(ltDist)
        u32Le(totalChunks)
        u64Le(rawSize)
        u64Le(encodedSize)
        u8(name.size)
        write(name)
        write(rawSha256)
    }.toByteArray()
}

fun DataPayload.pack(): ByteArray {
    if (adjacency.size != degree) throw ProtocolError("adjacency 长度与 degree 不符")
    return ByteArrayOutputStream().apply {
        u16Le(degree)
        adjacency.forEach { u32Le(it) }
        write(xorData)
    }.toByteArray()
}

fun EndPayload.pack(): ByteArray {
    if (rawSha256.size != Proto.SHA256_SIZE) throw ProtocolError("raw_sha256 必须 32 字节")
    return rawSha256.copyOf()
}

fun encodeFrame(header: FrameHeader, payload: ByteArray): ByteArray {
    val body = ByteArrayOutputStream().apply {
        u16Le(Proto.MAGIC)
        u8(header.version)
        u8(header.frameType)
        u32Le(header.sessionId)
        u32Le(header.streamId)
        u32Le(header.symbolId)
        u16Le(payload.size)
        write(payload)
    }.toByteArray()
    return ByteArrayOutputStream().apply {
        write(body)
        u32Le(crc32(body))
    }.toByteArray()
}
