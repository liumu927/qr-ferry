package com.qrferry.receiver.core.fixture

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.ByteArrayOutputStream
import java.util.zip.Inflater

/** protocol_v1.json 的 Kotlin 模型（字段名与 JSON 一一对应）。 */
@Serializable
data class ProtocolFixture(
    val description: String = "",
    @SerialName("generated_by") val generatedBy: String = "",
    val cases: List<Case>,
)

@Serializable
data class Case(
    val name: String,
    @SerialName("session_id") val sessionId: Int,
    @SerialName("content_type") val contentType: Int,
    val compression: Int,
    @SerialName("lt_dist") val ltDist: Int,
    @SerialName("chunk_size_log") val chunkSizeLog: Int,
    val redundancy: Double = 0.0,
    val rounds: Int = 0,
    val K: Int,
    @SerialName("raw_size") val rawSize: Long,
    @SerialName("encoded_size") val encodedSize: Long,
    @SerialName("raw_sha256") val rawSha256: String,
    @SerialName("input_hex") val inputHex: String,
    @SerialName("input_text") val inputText: String? = null,
    val filename: String = "",
    @SerialName("manifest_frame_hex") val manifestFrameHex: String,
    @SerialName("end_frame_hex") val endFrameHex: String,
    @SerialName("data_symbols") val dataSymbols: List<Symbol>,
)

@Serializable
data class Symbol(
    @SerialName("symbol_id") val symbolId: Int,
    val degree: Int,
    val adjacency: List<Int>,
    @SerialName("xor_data_hex") val xorDataHex: String,
    @SerialName("frame_hex") val frameHex: String,
)

/** 从 classpath 加载 protocol_v1.json。ignoreUnknownKeys 容忍 protocol 元信息字段。 */
object Fixtures {
    private val json = Json { ignoreUnknownKeys = true }

    fun load(): ProtocolFixture {
        val url = Thread.currentThread().contextClassLoader.getResource("protocol/protocol_v1.json")
            ?: error("未找到 classpath 资源 protocol/protocol_v1.json")
        return json.decodeFromString(url.readText())
    }
}

// ── 测试通用工具 ──────────────────────────────────────────

/** hex 字符串 → ByteArray。 */
fun hexToBytes(hex: String): ByteArray =
    ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }

/** ByteArray → 小写 hex 字符串（与 fixtures 一致）。 */
fun ByteArray.toHex(): String =
    joinToString("") { (it.toInt() and 0xFF).toString(16).padStart(2, '0') }

/** 解压标准 zlib 流（Python zlib.compress 默认产出；Inflater nowrap=false 可解）。 */
fun inflateZlib(data: ByteArray): ByteArray {
    val inf = Inflater()
    inf.setInput(data)
    val out = ByteArrayOutputStream()
    val buf = ByteArray(4096)
    while (!inf.finished()) {
        val n = inf.inflate(buf)
        if (n == 0) break
        out.write(buf, 0, n)
    }
    inf.end()
    return out.toByteArray()
}
