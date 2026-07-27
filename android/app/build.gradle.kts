// Android 接收端 app 模块：CameraX + ML Kit + protocol 库。
import java.io.FileInputStream
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

// release 签名：从 android/keystore.properties 读取（本地保管，不入库）。
// CI 无此文件时跳过 signingConfig，assembleDebug 不受影响。
val keystoreProperties = Properties().apply {
    rootProject.file("keystore.properties").takeIf { it.exists() }?.let { load(FileInputStream(it)) }
}

// 版本号与 git tag 联动：优先 -PversionName 或 QR_FERRY_VERSION 环境变量，否则 fallback 默认值。
// CI 在 tag 推送时注入 tag 名（去 v 前缀）；本地无注入时用默认值，assembleDebug 不受影响。
fun deriveVersionCode(name: String): Int {
    val parts = name.split(".").map { it.toIntOrNull() ?: 0 }
    val major = parts.getOrElse(0) { 0 }
    val minor = parts.getOrElse(1) { 0 }
    val patch = parts.getOrElse(2) { 0 }
    return major * 10000 + minor * 100 + patch
}

val releaseVersionName: String = (project.findProperty("versionName") as String?)
    ?: System.getenv("QR_FERRY_VERSION")?.takeIf { it.isNotBlank() }
    ?: "0.1.10"
val releaseVersionCode: Int = deriveVersionCode(releaseVersionName)

android {
    namespace = "com.qrferry.receiver"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.qrferry.receiver"
        minSdk = 26
        targetSdk = 34
        versionCode = releaseVersionCode
        versionName = releaseVersionName
    }

    if (keystoreProperties.containsKey("storeFile")) {
        signingConfigs {
            create("release") {
                storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (keystoreProperties.containsKey("storeFile")) {
                signingConfig = signingConfigs.getByName("release")
            }
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation(project(":protocol"))

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.ktx)

    // CameraX：预览 + 帧分析
    implementation(libs.camerax.core)
    implementation(libs.camerax.camera2)
    implementation(libs.camerax.lifecycle)
    implementation(libs.camerax.view)

    // QR 解码（单帧多码）
    implementation(libs.mlkit.barcode.scanning)
    // QR 编码（Android 发送端）
    implementation(libs.zxing.core)
}
