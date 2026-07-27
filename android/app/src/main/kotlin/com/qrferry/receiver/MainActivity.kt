package com.qrferry.receiver

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.SystemClock
import android.provider.OpenableColumns
import android.text.Editable
import android.text.TextWatcher
import android.util.Log
import android.util.Size
import android.view.View
import android.view.WindowManager
import android.webkit.MimeTypeMap
import android.graphics.drawable.BitmapDrawable
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import com.qrferry.receiver.databinding.ActivityMainBinding
import com.qrferry.receiver.pipeline.ReceivePipeline
import com.qrferry.receiver.send.ColorMatrixRenderer
import com.qrferry.receiver.send.QrRenderer
import com.qrferry.receiver.send.TextSendController
import kotlin.random.Random
import java.util.concurrent.Executors

/**
 * Layer 5 —— CameraX 预览 + ImageAnalysis（ML Kit 扫码）驱动 ReceivePipeline。
 * 分析流强制 1080p（2×2 网格下小 QR 仍可识别）。状态栏显示识别/有效/丢弃计数辅助诊断。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var pipeline: ReceivePipeline
    private lateinit var analysis: ImageAnalysis
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())
    // 发送渲染专用后台线程：帧生成+QR 编码渲染移出主线程，主线程只贴图。
    private val sendThread = HandlerThread("qr-send").apply { start() }
    private val sendHandler = Handler(sendThread.looper)
    private val colorRenderer = ColorMatrixRenderer()
    private val qrRenderer = QrRenderer()
    private var sendController: TextSendController? = null
    private var selectedFile: SelectedSendFile? = null
    private var sending = false
    private var previousScreenBrightness: Float? = null
    // 发送节拍（启动发送时快照，避免后台线程读 UI 状态）
    private var nextSendDeadline = 0L
    private var sendPeriodMs = 125L
    private var sendIsColor = false

    private val scanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build()
    )

    private var completed = false
    private var paused = false
    private var pendingReceivedFile: ReceivePipeline.Result.File? = null

    private val cameraPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startCamera() else setStatus("需要相机权限才能接收")
    }

    private val filePickerLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri ->
        if (uri != null) loadSelectedFile(uri)
    }

    private val saveFileLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { activityResult ->
        if (activityResult.resultCode != Activity.RESULT_OK) return@registerForActivityResult
        val uri = activityResult.data?.data ?: return@registerForActivityResult
        val file = pendingReceivedFile ?: return@registerForActivityResult
        try {
            contentResolver.openOutputStream(uri, "w")?.use { it.write(file.bytes) }
                ?: error("无法打开保存位置")
            val savedName = queryDisplayName(uri) ?: file.filename
            binding.resultText.setText("文件已保存：$savedName")
            setStatus("文件已保存：$savedName")
            Toast.makeText(this, "已保存 $savedName", Toast.LENGTH_LONG).show()
        } catch (e: Exception) {
            Toast.makeText(this, "文件保存失败: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        pipeline = ReceivePipeline()

        setupSendUi()
        binding.btnReset.setOnClickListener { reset() }
        binding.btnPause.setOnClickListener { togglePause() }
        binding.btnModeSend.setOnClickListener { showSendMode(true) }
        binding.btnModeReceive.setOnClickListener { showSendMode(false) }
        binding.btnCopyResult.setOnClickListener { copyResult() }
        binding.btnSaveResult.setOnClickListener { saveReceivedFile() }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            startCamera()
        } else {
            cameraPermissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private fun setupSendUi() {
        val codecAdapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_item,
            listOf("标准QR（推荐）", "彩色码（实验）")
        )
        codecAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        binding.sendCodecSpinner.adapter = codecAdapter

        val fpsAdapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_item,
            listOf("8 FPS", "12 FPS", "15 FPS")
        )
        fpsAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
        binding.sendFpsSpinner.adapter = fpsAdapter
        binding.sendTextInput.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                binding.sendMetaText.text = "${s?.length ?: 0} 字符"
                refreshSendEnabled()
            }
            override fun afterTextChanged(s: Editable?) = Unit
        })
        binding.sendCodecSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                refreshSendEnabled()
            }
            override fun onNothingSelected(parent: AdapterView<*>?) = Unit
        }
        binding.sendTypeToggle.addOnButtonCheckedListener { _, _, _ ->
            refreshSendTypeUi()
            refreshSendEnabled()
        }
        binding.btnClearSend.setOnClickListener {
            selectedFile = null
            binding.sendTextInput.text?.clear()
            binding.sendFileInfo.text = "未选择文件"
            refreshSendEnabled()
        }
        binding.btnPickFile.setOnClickListener {
            binding.sendTypeToggle.check(binding.btnSendFile.id)
            filePickerLauncher.launch(arrayOf("*/*"))
        }
        binding.btnSendFile.setOnClickListener {
            binding.sendTypeToggle.check(binding.btnSendFile.id)
            if (selectedFile == null) filePickerLauncher.launch(arrayOf("*/*"))
        }
        binding.btnStartSend.setOnClickListener { toggleSending() }
        showSendMode(false)
        refreshSendTypeUi()
        refreshSendEnabled()
    }

    private fun showSendMode(send: Boolean) {
        binding.sendPanel.visibility = if (send) View.VISIBLE else View.GONE
        binding.receivePanel.visibility = if (send) View.GONE else View.VISIBLE
    }

    private fun refreshSendEnabled() {
        val ready = if (isFileMode()) selectedFile != null else !binding.sendTextInput.text.isNullOrBlank()
        binding.btnStartSend.isEnabled = ready
        binding.sendStatusText.text = when {
            sending -> binding.sendStatusText.text
            ready -> "准备发送"
            isFileMode() -> "选择文件后可开始发送"
            else -> "输入文本后可开始发送"
        }
    }

    private fun refreshSendTypeUi() {
        val fileMode = isFileMode()
        binding.sendTextInput.visibility = if (fileMode) View.GONE else View.VISIBLE
        binding.sendMetaText.visibility = if (fileMode) View.GONE else View.VISIBLE
        binding.sendFileInfo.visibility = if (fileMode) View.VISIBLE else View.GONE
        binding.btnPickFile.isEnabled = !sending && fileMode
    }

    private fun toggleSending() {
        if (sending) {
            stopSending("已停止发送")
            return
        }
        val text = binding.sendTextInput.text?.toString().orEmpty()
        if (!isFileMode() && text.isBlank()) {
            Toast.makeText(this, "请输入要发送的文本", Toast.LENGTH_SHORT).show()
            return
        }
        try {
            val sid = Random.nextInt(1, Int.MAX_VALUE)
            val chunkSizeLog = if (isColorSend()) {
                TextSendController.COLOR_CHUNK_SIZE_LOG
            } else {
                TextSendController.QR_CHUNK_SIZE_LOG
            }
            val manifestInterval = if (isColorSend()) 8 else TextSendController.DEFAULT_MANIFEST_INTERVAL
            sendController = if (isFileMode()) {
                val file = selectedFile ?: run {
                    Toast.makeText(this, "请选择要发送的文件", Toast.LENGTH_SHORT).show()
                    return
                }
                TextSendController.forFile(
                    filename = file.name,
                    bytes = file.bytes,
                    sessionId = sid,
                    chunkSizeLog = chunkSizeLog,
                    manifestInterval = manifestInterval,
                )
            } else {
                TextSendController(
                    text,
                    sid,
                    chunkSizeLog = chunkSizeLog,
                    manifestInterval = manifestInterval,
                )
            }
            sending = true
            previousScreenBrightness = window.attributes.screenBrightness
            window.attributes = window.attributes.apply { screenBrightness = 1.0f }
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            binding.sendCodecSpinner.isEnabled = false
            binding.sendFpsSpinner.isEnabled = false
            binding.sendTypeToggle.isEnabled = false
            binding.sendTextInput.isEnabled = false
            binding.btnPickFile.isEnabled = false
            binding.btnClearSend.isEnabled = false
            binding.btnStartSend.text = "停止发送"
            binding.sendPlaceholder.visibility = View.GONE
            binding.sendStatusText.text = "发送中：K=${sendController?.K}，${selectedFps()} FPS"
            // 节拍锚定：渲染耗时不再叠加到标称间隔上；渲染慢于周期时自然顺延（不追赶连发）
            sendIsColor = isColorSend()
            sendPeriodMs = 1000L / selectedFps()
            nextSendDeadline = SystemClock.uptimeMillis()
            sendHandler.post(sendTick)
        } catch (e: Exception) {
            stopSending("发送初始化失败: ${e.message}")
        }
    }

    private val sendTick = object : Runnable {
        override fun run() {
            val ctrl = sendController
            if (!sending || ctrl == null) return
            try {
                // 后台线程：帧生成 + 渲染（ZXing 编码耗时从这里移除主线程）
                val frame = ctrl.nextFrame()
                val bitmap = if (sendIsColor) colorRenderer.render(frame) else qrRenderer.render(frame)
                val status = "发送中：K=${ctrl.K} sid=${ctrl.sessionId} next=${ctrl.nextSymbolId} raw=${ctrl.rawSize}B"
                mainHandler.post {
                    if (!sending) return@post
                    binding.sendCodeView.setImageDrawable(
                        BitmapDrawable(resources, bitmap).apply { isFilterBitmap = false }
                    )
                    binding.sendStatusText.text = status
                }
                nextSendDeadline = maxOf(nextSendDeadline + sendPeriodMs, SystemClock.uptimeMillis())
                sendHandler.postAtTime(this, nextSendDeadline)
            } catch (e: Exception) {
                mainHandler.post { stopSending("发送失败: ${e.message}") }
            }
        }
    }

    private fun selectedFps(): Int = when (binding.sendFpsSpinner.selectedItemPosition) {
        0 -> 8
        2 -> 15
        else -> 12
    }

    private fun stopSending(message: String) {
        sending = false
        previousScreenBrightness?.let { brightness ->
            window.attributes = window.attributes.apply { screenBrightness = brightness }
        }
        previousScreenBrightness = null
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        sendController = null
        sendHandler.removeCallbacks(sendTick)
        binding.btnStartSend.text = "开始发送"
        binding.sendCodecSpinner.isEnabled = true
        binding.sendFpsSpinner.isEnabled = true
        binding.sendTypeToggle.isEnabled = true
        binding.sendTextInput.isEnabled = true
        binding.btnClearSend.isEnabled = true
        binding.sendStatusText.text = message
        refreshSendTypeUi()
        refreshSendEnabled()
    }

    private fun isColorSend(): Boolean = binding.sendCodecSpinner.selectedItemPosition == 1

    private fun isFileMode(): Boolean = binding.sendTypeToggle.checkedButtonId == binding.btnSendFile.id

    private fun loadSelectedFile(uri: Uri) {
        try {
            val name = trimFilename(queryDisplayName(uri) ?: "qrferry.bin")
            val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() }
                ?: error("无法读取文件")
            selectedFile = SelectedSendFile(name, bytes)
            binding.sendTypeToggle.check(binding.btnSendFile.id)
            binding.sendFileInfo.text = "$name · ${formatBytes(bytes.size.toLong())}"
            refreshSendTypeUi()
            refreshSendEnabled()
        } catch (e: Exception) {
            selectedFile = null
            binding.sendFileInfo.text = "文件读取失败"
            refreshSendEnabled()
            Toast.makeText(this, "文件读取失败: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun queryDisplayName(uri: Uri): String? {
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (index >= 0) return cursor.getString(index)
            }
        }
        return uri.lastPathSegment
    }

    private fun trimFilename(name: String): String {
        var out = name.ifBlank { "qrferry.bin" }
        while (out.toByteArray(Charsets.UTF_8).size > 255) {
            out = out.dropLast(1)
        }
        return out.ifBlank { "qrferry.bin" }
    }

    private fun formatBytes(bytes: Long): String = when {
        bytes >= 1024L * 1024L -> "%.2f MB".format(bytes / 1024.0 / 1024.0)
        bytes >= 1024L -> "%.1f KB".format(bytes / 1024.0)
        else -> "$bytes B"
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = future.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(binding.previewView.surfaceProvider)
            }
            @Suppress("DEPRECATION")
            analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .setTargetResolution(Size(1920, 1080))
                .build()
            if (!paused) analysis.setAnalyzer(analysisExecutor, ::analyzeImage)
            try {
                provider.unbindAll()
                provider.bindToLifecycle(
                    this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis
                )
                setStatus("相机就绪（1080p 分析流）。对准 PC 发送端 QR…")
            } catch (e: Exception) {
                setStatus("相机启动失败: ${e.message}")
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun reset() {
        pipeline = ReceivePipeline()
        completed = false
        pendingReceivedFile = null
        binding.resultText.setText("")
        binding.btnCopyResult.visibility = View.GONE
        binding.btnSaveResult.visibility = View.GONE
        if (!paused) analysis.setAnalyzer(analysisExecutor, ::analyzeImage)
        setStatus("已重置。对准 QR 码…")
        Toast.makeText(this, "已重置", Toast.LENGTH_SHORT).show()
    }

    private fun togglePause() {
        paused = !paused
        if (paused) {
            analysis.clearAnalyzer()
            binding.btnPause.text = "继续"
        } else {
            analysis.setAnalyzer(analysisExecutor, ::analyzeImage)
            binding.btnPause.text = "暂停"
        }
    }

    @OptIn(ExperimentalGetImage::class)
    private fun analyzeImage(imageProxy: ImageProxy) {
        val mediaImage = imageProxy.image
        if (mediaImage == null || completed) {
            imageProxy.close()
            return
        }
        val input = InputImage.fromMediaImage(mediaImage, imageProxy.imageInfo.rotationDegrees)
        scanner.process(input)
            .addOnSuccessListener { barcodes ->
                if (barcodes.isNotEmpty()) {
                    Log.d(TAG, "识别到 ${barcodes.size} 个码")
                }
                if (completed) return@addOnSuccessListener
                if (pipeline.process(barcodes)) {
                    completed = true
                    runOnUiThread { onCompleted() }
                } else if (pipeline.started || barcodes.isNotEmpty()) {
                    updateStatus()
                }
            }
            .addOnCompleteListener { imageProxy.close() }
    }

    private fun updateStatus() {
        val p = pipeline
        val m = p.manifest
        val pct = if (p.K > 0) (p.progress * 100).toInt() else 0
        val missing = p.missingIndices
        val missingStr = if (missing.size <= 20) missing.toString() else "[${missing.size} 块]"
        val elapsed = if (p.elapsedSeconds > 0.0) " · 已用 %.1fs".format(p.elapsedSeconds) else ""
        val head = if (m != null) {
            "接收中… ${pct}% | 缺块 $missingStr\n" +
                "K=${p.K} content=${m.contentType} comp=${m.compression}\n"
        } else {
            "等待 MANIFEST…（先收到 MANIFEST 才开始）\n"
        }
        runOnUiThread {
            setStatus(head + "识别 ${p.seenBarcodes} · 有效 ${p.validFrames} · 丢弃 ${p.badFrames}$elapsed")
        }
    }

    private fun onCompleted() {
        val result = try {
            pipeline.finalize()
        } catch (e: Exception) {
            setStatus("重组失败: ${e.message}")
            return
        }
        val sb = StringBuilder()
        sb.append("✅ 接收完成\n")
        sb.append("SHA-256: ${if (result.shaOk) "通过 ✓" else "失败 ✗"}\n")
        sb.append(result.shaHex).append("\n\n")
        when (result) {
            is ReceivePipeline.Result.Text -> {
                pendingReceivedFile = null
                sb.append("[TEXT]\n").append(result.text)
                binding.resultText.setText(result.text)
                binding.btnCopyResult.visibility = View.VISIBLE
                binding.btnSaveResult.visibility = View.GONE
                setStatus(sb.toString())
            }
            is ReceivePipeline.Result.File -> {
                pendingReceivedFile = result
                sb.append("[FILE] ${result.filename} (${result.bytes.size} B)\n")
                sb.append("等待用户选择保存位置")
                binding.resultText.setText("文件接收完成：${result.filename}\n大小：${formatBytes(result.bytes.size.toLong())}")
                binding.btnCopyResult.visibility = View.GONE
                binding.btnSaveResult.visibility = View.VISIBLE
                setStatus(sb.toString())
            }
        }
    }

    private fun saveReceivedFile() {
        val file = pendingReceivedFile ?: return
        val extension = file.filename.substringAfterLast('.', "").lowercase()
        val mimeType = extension.takeIf { it.isNotBlank() }
            ?.let { MimeTypeMap.getSingleton().getMimeTypeFromExtension(it) }
            ?: "application/octet-stream"
        val intent = Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = mimeType
            putExtra(Intent.EXTRA_TITLE, file.filename)
        }
        saveFileLauncher.launch(intent)
    }

    private fun setStatus(text: String) {
        binding.statusText.text = text
    }

    private fun copyResult() {
        val text = binding.resultText.text?.toString().orEmpty()
        if (text.isBlank()) {
            Toast.makeText(this, "没有可复制的结果", Toast.LENGTH_SHORT).show()
            return
        }
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("qr-ferry", text))
        Toast.makeText(this, "已复制", Toast.LENGTH_SHORT).show()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopSending("已停止发送")
        analysisExecutor.shutdown()
        sendThread.quitSafely()
        scanner.close()
    }

    companion object {
        private const val TAG = "qr-ferry-recv"
    }

    private data class SelectedSendFile(
        val name: String,
        val bytes: ByteArray,
    )
}
