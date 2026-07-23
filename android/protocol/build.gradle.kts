// 协议层模块：纯 Kotlin/JVM，零 Android 依赖，可独立 JVM 单测。
// 逐字节对齐 qr-ferry 协议 v1.0（参考 src/qrferry/core 的 Python 实现）。
plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

group = "com.qrferry"
version = "0.1.0"

kotlin {
    jvmToolchain(17)
}

dependencies {
    // kotlinx-serialization 仅测试用（解析 fixtures）；主协议代码零第三方依赖。
    testImplementation(libs.kotlinx.serialization.json)
    testImplementation(libs.junit.jupiter)
    testRuntimeOnly(libs.junit.platform.launcher)
}

tasks.test {
    useJUnitPlatform()
}
