package com.qrferry.receiver

import android.Manifest
import android.app.Activity
import android.app.Dialog
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
import android.view.inputmethod.InputMethodManager
import android.webkit.MimeTypeMap
import android.graphics.drawable.BitmapDrawable
import android.widget.ArrayAdapter
import android.widget.ImageView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
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
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel
import com.qrferry.receiver.core.Compression
import com.qrferry.receiver.core.ContentType
import com.qrferry.receiver.core.LtEncoder
import com.qrferry.receiver.databinding.ActivityMainBinding
import com.qrferry.receiver.pipeline.ReceivePipeline
import com.qrferry.receiver.send.ColorMatrixRenderer
import com.qrferry.receiver.send.FileOptimizer
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
    private var rawSelectedFile: SelectedSendFile? = null   // 优化前的原始文件（取消勾选时还原）
    private var optimizeToken = 0                           // 异步优化竞态令牌
    private var optimizeRunning = false                     // 串行单飞：同时只允许一个优化线程
    private var optimizePending = false                     // 优化期间又有新选择 → 完成后重跑最新
    private var sending = false
    private var previousScreenBrightness: Float? = null
    // 全屏遮罩（发送「全屏展示」勾选时使用）：黑底 Dialog + 大 QR，点击/返回可退出（发送继续）
    private var fullscreenDialog: Dialog? = null
    private var fullscreenCodeView: ImageView? = null
    // 发送节拍（启动发送时快照，避免后台线程读 UI 状态）
    private var nextSendDeadline = 0L
    private var sendPeriodMs = 125L
    private var sendIsColor = false
    private var sendEcLevel = ErrorCorrectionLevel.L
    // 发送参数下拉选中项（AutoCompleteTextView 无 selectedItemPosition，改用字段保存）
    private var codecPosition = 0
    private var fpsPosition = FPS_OPTIONS.indexOf(DEFAULT_FPS)
    private var frameBytesPosition = FRAME_BYTES_OPTIONS.indexOf(DEFAULT_FRAME_BYTES).coerceAtLeast(0)
    private var ecPosition = 0

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
        // 副标题随构建版本显示（BuildConfig.VERSION_NAME 与 gradle versionName 同步）
        binding.subtitleText.text = "光学二维码摆渡 · v${BuildConfig.VERSION_NAME}"

        setupSendUi()
        binding.btnHelp.setOnClickListener { showHelp() }
        binding.btnSendParamHelp.setOnClickListener { showParamHelp() }
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

    /** 标题栏「?」按钮：弹窗展示普通用户向的简明使用说明。 */
    private fun showHelp() {
        AlertDialog.Builder(this)
            .setTitle("使用说明 · qr-ferry")
            .setMessage(
                "一、发送（屏幕显示二维码，另一端扫码接收）\n" +
                    "1. 选「文件」或「文本」，准备好内容；\n" +
                    "2. 点「开始发送」，手机屏幕显示二维码；\n" +
                    "3. 另一端（电脑或手机）的摄像头对准扫码即可，发送中可随时停止。\n\n" +
                    "二、接收（用摄像头扫描另一端屏幕）\n" +
                    "1. 切到「接收」页，把手机摄像头对准另一端屏幕；\n" +
                    "2. 保持 15~30cm 距离，让二维码在画面内清晰对焦；\n" +
                    "3. 完成后文本可直接复制，文件点「保存文件」选择位置。\n\n" +
                    "三、小提示\n" +
                    "· 设备不限：电脑与电脑、手机与手机、电脑与手机都可以互传；\n" +
                    "· 二维码与摄像头保持 15~30cm，光线均匀、对焦清晰成功率最高；\n" +
                    "· 发送较慢时调低帧率（如 8 FPS）更稳，调高帧率（如 24/30）更快；\n" +
                    "· 默认帧长 1200B + 12fps；追求更快可上调到 2200B（需接收端跑 60fps），\n" +
                    "  对方摄像头较旧扫不出则降到 700B；\n" +
                    "· 传输中断可重新点「开始发送/接收」，已收到的部分会自动续传；\n" +
                    "· 发送时屏幕会保持常亮，结束后自动恢复。"
            )
            .setPositiveButton("知道了", null)
            .show()
    }

    /** 参数区叹号：集中说明各参数含义（普通用户向）。 */
    private fun showParamHelp() {
        AlertDialog.Builder(this)
            .setTitle("参数说明")
            .setMessage(
                "码制：当前版本固定使用标准 QR（兼容性最好）。彩色码为实验功能，因尚未通过验证暂不提供。\n\n" +
                    "帧率：每秒显示多少张二维码。越高传得越快，但码切换太快容易扫不准；" +
                    "扫不稳时调低（如 8 FPS）更稳。\n\n" +
                    "帧长：每张二维码携带的数据量。默认 1200B，兼顾速度与兼容性。\n" +
                    "接收端采集能跑 60fps 时（曝光 ≤16.7ms，画面不撕裂）可上调到 2200B，吞吐约为 700B 的 2.6 倍；\n" +
                    "若对方只支持 30fps 或摄像头较旧扫不出，请降到 700B。\n\n" +
                    "纠错：二维码的容错等级。越高越耐遮挡、模糊，但码点更密、更难扫；" +
                    "一般用 L 或 M 即可。"
            )
            .setPositiveButton("知道了", null)
            .show()
    }

    private fun setupSendUi() {
        // 四个下拉框：ExposedDropdownMenu（AutoCompleteTextView），选中项存字段
        // 彩色码（实验）未通过真机验证，暂不从界面暴露。
        // 底层实现（ColorMatrixRenderer、isColorSend 分支、帧长锁定）完整保留，
        // 验证通过后把 "彩色码（实验）" 加回 CODEC_OPTIONS 即可恢复。
        val codecOptions = CODEC_OPTIONS
        binding.sendCodecInput.setAdapter(
            ArrayAdapter(this, android.R.layout.simple_list_item_1, codecOptions))
        binding.sendCodecInput.setText(codecOptions[codecPosition], false)
        // 只有一个码制时禁用下拉：可点开却无其它选项会让人以为界面坏了
        binding.sendCodecInput.isEnabled = codecOptions.size > 1
        binding.sendCodecInput.setOnItemClickListener { _, _, position, _ ->
            codecPosition = position
            refreshFrameBytesForCodec()
            refreshSendEnabled()
        }

        val fpsOptions = FPS_OPTIONS.map { "$it FPS" }
        binding.sendFpsInput.setAdapter(
            ArrayAdapter(this, android.R.layout.simple_list_item_1, fpsOptions))
        binding.sendFpsInput.setText(fpsOptions[fpsPosition], false)
        binding.sendFpsInput.setOnItemClickListener { _, _, position, _ ->
            fpsPosition = position
        }

        val frameBytesOptions = FRAME_BYTES_OPTIONS.map { "帧长 $it B" }
        binding.sendFrameBytesInput.setAdapter(
            ArrayAdapter(this, android.R.layout.simple_list_item_1, frameBytesOptions))
        binding.sendFrameBytesInput.setText(frameBytesOptions[frameBytesPosition], false)
        binding.sendFrameBytesInput.setOnItemClickListener { _, _, position, _ ->
            frameBytesPosition = position
        }

        val ecOptions = listOf("纠错 L", "纠错 M")
        binding.sendEcInput.setAdapter(
            ArrayAdapter(this, android.R.layout.simple_list_item_1, ecOptions))
        binding.sendEcInput.setText(ecOptions[ecPosition], false)
        binding.sendEcInput.setOnItemClickListener { _, _, position, _ ->
            ecPosition = position
        }
        binding.sendTextInput.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                binding.sendMetaText.text = "${s?.length ?: 0} 字符"
                refreshSendEnabled()
            }
            override fun afterTextChanged(s: Editable?) = Unit
        })
        binding.sendTypeToggle.addOnButtonCheckedListener { _, _, _ ->
            refreshSendTypeUi()
            refreshSendEnabled()
        }
        binding.btnClearSend.setOnClickListener {
            selectedFile = null
            rawSelectedFile = null
            optimizeToken++
            binding.sendTextInput.text?.clear()
            binding.sendFileInfo.text = "未选择文件"
            refreshSendEnabled()
        }
        binding.btnClearSendText.setOnClickListener {
            selectedFile = null
            rawSelectedFile = null
            optimizeToken++
            binding.sendTextInput.text?.clear()
            binding.sendFileInfo.text = "未选择文件"
            refreshSendEnabled()
        }
        binding.btnStartSendText.setOnClickListener { toggleSending() }
        binding.sendOptimizeCheck.setOnCheckedChangeListener { _, _ ->
            if (rawSelectedFile != null) applyOptimization()
        }
        // 圆形叹号：点击才展示括号内的说明，避免「接收文件会不同」常驻造成歧义
        binding.btnOptimizeInfo.setOnClickListener {
            AlertDialog.Builder(this)
                .setTitle("允许有损优化")
                .setMessage(
                    "无损优化自动进行：DOCX/XLSX/PPTX 重打包、PDF 流压缩，不改变文件内容。\n\n" +
                        "勾选后额外允许有损转码：照片 PNG→JPEG、截图调色板量化、OOXML 内嵌图片压缩等——" +
                        "接收到的文件与原文件不同（格式/质量/透明通道可能变化）。"
                )
                .setPositiveButton("知道了", null)
                .show()
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
        showSendMode(true)   // 默认进入发送页
        applyDefaultSendParams()   // 仅此处套默认值；停止发送/选文件不再重置
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
        binding.btnStartSendText.isEnabled = ready
        // 就绪提示与文件状态同行（sendReadyHint 右侧）；sendStatusText 仅承载发送中/结果状态
        binding.sendReadyHint.text = when {
            sending -> binding.sendReadyHint.text
            ready -> "准备发送"
            isFileMode() -> "选择文件后可开始发送"
            else -> "输入文本后可开始发送"
        }
    }

    /** 彩色码（实验）走固定帧长，此时把下拉框禁用并显示真实值；
     *  切回标准 QR 恢复用户原先选择——否则「显示 2200B、实际发 720B」会误导排障。 */
    private fun refreshFrameBytesForCodec() {
        if (isColorSend()) {
            binding.sendFrameBytesInput.isEnabled = false
            binding.sendFrameBytesInput.setText(
                "帧长 ${LtEncoder.DEFAULT_MAX_FRAME_BYTES} B（彩色码固定）", false)
        } else {
            binding.sendFrameBytesInput.isEnabled = !sending
            binding.sendFrameBytesInput.setText(
                "帧长 ${FRAME_BYTES_OPTIONS[frameBytesPosition]} B", false)
        }
    }

    private fun refreshSendTypeUi() {
        val fileMode = isFileMode()
        binding.sendTextInput.visibility = if (fileMode) View.GONE else View.VISIBLE
        binding.sendMetaText.visibility = if (fileMode) View.GONE else View.VISIBLE
        binding.sendFileInfo.visibility = if (fileMode) View.VISIBLE else View.GONE
        binding.sendOptimizeCheck.visibility = if (fileMode) View.VISIBLE else View.GONE
        binding.btnOptimizeInfo.visibility = if (fileMode) View.VISIBLE else View.GONE
        // 左列容器必须一起隐藏：只隐藏子控件时容器仍按 weight=1 占掉左半宽，
        // 「二维码全屏展示」会被挤到右半区、左边空一大块（看起来像悬在中间）。
        binding.optimizeGroup.visibility = if (fileMode) View.VISIBLE else View.GONE
        // 文件模式：两行按钮（选择文件/清空 + 开始发送）；
        // 文本模式：一行（清空内容/开始发送）
        // 注：发送端无断点续传，勿再添加「恢复发送」类按钮（PC 端续传已并入开始发送）
        binding.fileButtonsRow1.visibility = if (fileMode) View.VISIBLE else View.GONE
        binding.fileButtonsRow2.visibility = if (fileMode) View.VISIBLE else View.GONE
        binding.textButtonsRow.visibility = if (fileMode) View.GONE else View.VISIBLE
        binding.btnPickFile.isEnabled = !sending
    }

    /** 首次进入时套用一次默认发送参数（帧长 1200B / 帧率 12fps）。
     *
     * 只初始化时调用：此前这段逻辑写在 refreshSendTypeUi() 里，而该函数在「停止发送」
     * 「选中文件」时都会跑，导致每次停下来参数就被打回默认——用户刚手调的那套参数
     * 立刻消失，无从确认上次用的是哪组档位。参数属于用户选择，必须持久保留。
     */
    private fun applyDefaultSendParams() {
        val defaultBytesIdx = FRAME_BYTES_OPTIONS.indexOf(DEFAULT_FRAME_BYTES).coerceAtLeast(0)
        frameBytesPosition = defaultBytesIdx
        binding.sendFrameBytesInput.setText(
            "帧长 ${FRAME_BYTES_OPTIONS[defaultBytesIdx]} B", false)
        val defaultFpsIdx = FPS_OPTIONS.indexOf(DEFAULT_FPS).coerceAtLeast(0)
        fpsPosition = defaultFpsIdx
        binding.sendFpsInput.setText("${FPS_OPTIONS[defaultFpsIdx]} FPS", false)
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
            // 帧长选项仅作用于标准 QR；彩色码（实验）路径保持原默认上限。
            val maxFrameBytes = if (isColorSend()) {
                LtEncoder.DEFAULT_MAX_FRAME_BYTES
            } else {
                selectedFrameBytes()
            }
            // 每帧有效载荷≈源块大小，源块档位随帧长联动（700→512B、1200→1024B、2200→2048B）。
            val chunkSizeLog = if (isColorSend()) {
                TextSendController.COLOR_CHUNK_SIZE_LOG
            } else {
                TextSendController.chunkSizeLogForFrameBytes(maxFrameBytes)
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
                    maxFrameBytes = maxFrameBytes,
                )
            } else {
                TextSendController(
                    text,
                    sid,
                    chunkSizeLog = chunkSizeLog,
                    manifestInterval = manifestInterval,
                    maxFrameBytes = maxFrameBytes,
                )
            }
            sending = true
            // 收起软键盘并失焦：发送时 QR 全尺寸显示，避免被键盘压缩/遮挡影响接收端扫码
            val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
            imm.hideSoftInputFromWindow(binding.sendTextInput.windowToken, 0)
            binding.sendTextInput.clearFocus()
            previousScreenBrightness = window.attributes.screenBrightness
            window.attributes = window.attributes.apply { screenBrightness = 1.0f }
            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            binding.sendCodecInput.isEnabled = false
            binding.sendFpsInput.isEnabled = false
            binding.sendFrameBytesInput.isEnabled = false
            binding.sendEcInput.isEnabled = false
            binding.sendTypeToggle.isEnabled = false
            binding.sendTextInput.isEnabled = false
            binding.btnPickFile.isEnabled = false
            binding.btnClearSend.isEnabled = false
            binding.btnClearSendText.isEnabled = false
            binding.sendOptimizeCheck.isEnabled = false
            binding.sendFullscreenCheck.isEnabled = false
            binding.btnStartSend.text = "停止发送"
            binding.btnStartSendText.text = "停止发送"
            binding.sendPlaceholder.visibility = View.GONE
            binding.sendStatusText.text = "发送中：K=${sendController?.K}，${selectedFps()} FPS"
            showFullscreenOverlayIfEnabled()
            // 节拍锚定：渲染耗时不再叠加到标称间隔上；渲染慢于周期时自然顺延（不追赶连发）
            sendIsColor = isColorSend()
            sendEcLevel = selectedEcLevel()
            sendPeriodMs = 1000L / selectedFps()
            nextSendDeadline = SystemClock.uptimeMillis()
            sendHandler.post(sendTick)
        } catch (e: Throwable) {
            // 捕获 Throwable（含 OOM）：初始化失败降级为可恢复状态，绝不闪退
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
                val bitmap = if (sendIsColor) {
                    colorRenderer.render(frame)
                } else {
                    qrRenderer.render(frame, sendEcLevel)
                }
                val status = "发送中：K=${ctrl.K} sid=${ctrl.sessionId} next=${ctrl.nextSymbolId} raw=${ctrl.rawSize}B"
                mainHandler.post {
                    if (!sending) return@post
                    val drawable = BitmapDrawable(resources, bitmap).apply { isFilterBitmap = false }
                    binding.sendCodeView.setImageDrawable(drawable)
                    fullscreenCodeView?.setImageDrawable(drawable)   // 全屏遮罩同步（若可见）
                    binding.sendStatusText.text = status
                }
                nextSendDeadline = maxOf(nextSendDeadline + sendPeriodMs, SystemClock.uptimeMillis())
                sendHandler.postAtTime(this, nextSendDeadline)
            } catch (e: Exception) {
                mainHandler.post { stopSending("发送失败: ${e.message}") }
            }
        }
    }

    private fun selectedFps(): Int =
        FPS_OPTIONS.getOrElse(fpsPosition) { DEFAULT_FPS }

    private fun selectedFrameBytes(): Int =
        FRAME_BYTES_OPTIONS.getOrElse(frameBytesPosition) {
            LtEncoder.DEFAULT_MAX_FRAME_BYTES
        }

    private fun selectedEcLevel(): ErrorCorrectionLevel =
        if (ecPosition == 1) {
            ErrorCorrectionLevel.M
        } else {
            ErrorCorrectionLevel.L
        }

    private fun stopSending(message: String) {
        sending = false
        closeFullscreenOverlay()
        previousScreenBrightness?.let { brightness ->
            window.attributes = window.attributes.apply { screenBrightness = brightness }
        }
        previousScreenBrightness = null
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        sendController = null
        sendHandler.removeCallbacks(sendTick)
        binding.btnStartSend.text = "开始发送"
        binding.btnStartSendText.text = "开始发送"
        binding.sendCodecInput.isEnabled = CODEC_OPTIONS.size > 1   // 仅一个码制时保持禁用
        binding.sendFpsInput.isEnabled = true
        binding.sendEcInput.isEnabled = true
        binding.sendTypeToggle.isEnabled = true
        binding.sendTextInput.isEnabled = true
        binding.btnClearSend.isEnabled = true
        binding.btnClearSendText.isEnabled = true
        binding.sendOptimizeCheck.isEnabled = true
        binding.sendFullscreenCheck.isEnabled = true
        binding.sendStatusText.text = message
        // 帧长下拉的启用状态与显示文本由码制决定（彩色码固定 700B），
        // 且**不得**在这里重置成默认值——用户刚用的那套参数要保持原样。
        refreshFrameBytesForCodec()
        refreshSendTypeUi()
        refreshSendEnabled()
    }

    // ────────────── 全屏遮罩（发送「全屏展示」勾选时）──────────────
    /** 勾选「全屏展示」且正在发送时：弹出全屏黑底遮罩，QR 码体最大化显示。 */
    private fun showFullscreenOverlayIfEnabled() {
        if (!binding.sendFullscreenCheck.isChecked) return
        if (fullscreenDialog != null) return
        val dialog = Dialog(this, android.R.style.Theme_Black_NoTitleBar_Fullscreen)
        dialog.window?.addFlags(
            WindowManager.LayoutParams.FLAG_FULLSCREEN or
                WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
        )
        val codeView = ImageView(this).apply {
            adjustViewBounds = true
            scaleType = ImageView.ScaleType.FIT_CENTER
            setBackgroundColor(android.graphics.Color.BLACK)
        }
        dialog.setContentView(codeView)
        // 点击遮罩任意处 → 退出遮罩（发送继续，主界面 QR 仍实时显示）
        codeView.setOnClickListener { dialog.dismiss() }
        dialog.setOnDismissListener { fullscreenDialog = null; fullscreenCodeView = null }
        fullscreenDialog = dialog
        fullscreenCodeView = codeView
        // 先铺一帧（发送 tick 已有最新帧时同步更新）
        binding.sendCodeView.drawable?.let { codeView.setImageDrawable(it) }
        dialog.show()
    }

    /** 关闭全屏遮罩（停止发送/页面销毁时调用；点击遮罩退出走 setOnDismissListener 清理）。 */
    private fun closeFullscreenOverlay() {
        fullscreenDialog?.dismiss()
        fullscreenDialog = null
        fullscreenCodeView = null
    }

    private fun isColorSend(): Boolean = codecPosition == 1

    private fun isFileMode(): Boolean = binding.sendTypeToggle.checkedButtonId == binding.btnSendFile.id

    private fun loadSelectedFile(uri: Uri) {
        try {
            val name = trimFilename(queryDisplayName(uri) ?: "qrferry.bin")
            val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() }
                ?: error("无法读取文件")
            rawSelectedFile = SelectedSendFile(name, bytes)
            binding.sendTypeToggle.check(binding.btnSendFile.id)
            applyOptimization()
            refreshSendTypeUi()
            refreshSendEnabled()
        } catch (e: Exception) {
            selectedFile = null
            rawSelectedFile = null
            binding.sendFileInfo.text = "文件读取失败"
            refreshSendEnabled()
            Toast.makeText(this, "文件读取失败: ${e.message}", Toast.LENGTH_LONG).show()
        }
    }

    /**
     * 自动文件优化：选择文件后即后台做**无损**再编码（OOXML 重打包等，内容不变）；
     * 勾选「允许有损优化」时额外允许照片转 JPEG / 降位等**有损**转码（接收文件会不同）。
     * 仅当结果更小时生效。
     *
     * 健壮性（v0.1.16 修复）：
     * - 串行单飞：一次只允许一个优化线程，新选择先挂 pending，避免多次选文件并发
     *   Bitmap 解码叠加内存 → OutOfMemoryError 闪退；
     * - 捕获 Throwable（含 OOM）：优化失败回退原文件，绝不崩溃；
     * - 优化期间 selectedFile=null 且完成后 refreshSendEnabled()——修复"开始按钮
     *   不亮/发送旧文件"的竞态；
     * - optimizeToken 递增校验：丢弃过期线程结果。
     */
    private fun applyOptimization() {
        val raw = rawSelectedFile ?: return
        val token = ++optimizeToken
        val lossy = binding.sendOptimizeCheck.isChecked
        if (optimizeRunning) {
            optimizePending = true   // 最新选择挂起，当前线程结束后重跑
            return
        }
        optimizeRunning = true
        selectedFile = null          // 优化期间禁用开始（防止发送旧文件）
        binding.sendFileInfo.text = "优化中…"
        refreshSendEnabled()
        Thread {
            var failed = false
            val res = try {
                FileOptimizer.optimize(raw.bytes, raw.name, lossy = lossy)
            } catch (t: Throwable) {
                failed = true
                null
            }
            mainHandler.post {
                optimizeRunning = false
                val rerun = optimizePending
                optimizePending = false
                if (rerun) {
                    applyOptimization()
                    return@post
                }
                if (token != optimizeToken || rawSelectedFile !== raw) return@post
                when {
                    res != null -> {
                        selectedFile = SelectedSendFile(res.filename, res.data)
                        binding.sendFileInfo.text =
                            "${raw.name} → ${res.filename}\n${res.description}"
                    }
                    failed -> {
                        selectedFile = raw
                        binding.sendFileInfo.text =
                            "${raw.name} · ${formatBytes(raw.bytes.size.toLong())}（优化失败，已使用原文件）"
                    }
                    else -> {
                        selectedFile = raw
                        binding.sendFileInfo.text =
                            "${raw.name} · ${formatBytes(raw.bytes.size.toLong())}（无优化收益）"
                    }
                }
                refreshSendEnabled()
            }
        }.start()
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

    private fun formatEta(seconds: Double): String {
        val s = maxOf(0L, seconds.toLong())
        if (s < 60) return "${s}s"
        val min = s / 60
        val sec = s % 60
        if (min < 60) return if (sec > 0) "${min}m${"%02d".format(sec)}s" else "${min}m"
        val h = min / 60
        val mm = min % 60
        return if (mm > 0) "${h}h${"%02d".format(mm)}m" else "${h}h"
    }

    private fun updateStatus() {
        val p = pipeline
        val m = p.manifest
        val pct = if (p.K > 0) (p.progress * 100).toInt() else 0
        val missing = p.missingIndices
        val missingStr = if (missing.size <= 20) missing.toString() else "[${missing.size} 块]"
        val etaStr = p.etaSeconds?.let { formatEta(it) } ?: "—"
        val head = if (m != null) {
            val compName = Compression.of(m.compression)?.name ?: "?${m.compression}"
            val typeName = ContentType.of(m.contentType)?.name ?: "?${m.contentType}"
            "接收中… ${pct}% | 缺块 $missingStr | ETA $etaStr\n" +
                "速率 %.1f KB/s（均 %.1f） · %.1f fps\n".format(
                    p.linkBpsInstant / 1024.0, p.linkBpsAvg / 1024.0, p.fpsInstant) +
                "K=${p.K} type=${typeName} comp=$compName\n"
        } else {
            "等待 MANIFEST…（先收到 MANIFEST 才开始）\n"
        }
        runOnUiThread {
            setStatus(head + "识别 ${p.seenBarcodes} · 有效 ${p.validDataFrames} · 丢弃 ${p.badFrames}" +
                " · 已用 %.1fs".format(p.elapsedSeconds))
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
                binding.resultText.setText(result.text)
                binding.btnCopyResult.visibility = View.VISIBLE
                binding.btnSaveResult.visibility = View.GONE
                // 状态框（黑底）只展示摘要，不重复完整文本内容（内容见 resultText 文本框）
                setStatus("✅ 接收完成 · 文本（${result.text.length} 字符）\nSHA-256: ${if (result.shaOk) "通过 ✓" else "失败 ✗"}")
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
        // 帧率档位（精简为 5 档：低帧率稳、高帧率快，中档 10/15/18/20 与相邻档差异不明显）
        private val FPS_OPTIONS = listOf(5, 8, 12, 24, 30)
        // 彩色码（实验）未通过真机验证，暂不暴露；恢复时加回 "彩色码（实验）" 即可。
        private val CODEC_OPTIONS = listOf("标准QR（推荐）")
        // 默认 12fps：700B 帧长下比 8fps 多 50% 吞吐，同时每张码仍显示 83ms，
        // 普通摄像头（30fps 采集）约有 2~3 次识别机会，不会因切换过快而扫不稳。
        private const val DEFAULT_FPS = 12
        private val FRAME_BYTES_OPTIONS = listOf(700, 1200, 2200)
        // 默认帧长 1200B：折中档——比 700B 快（源块 1024B vs 512B），又不像 2200B 那样
        // 要求接收端必须跑在 60fps（曝光 ≤16.7ms）才稳。文件与文本共用此默认值。
        private const val DEFAULT_FRAME_BYTES = 1200
    }

    private data class SelectedSendFile(
        val name: String,
        val bytes: ByteArray,
    )
}
