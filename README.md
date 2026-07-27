# qr-ferry

> 光学二维码单向摆渡传输系统 · PC 端（Python）+ Android 端（Kotlin）

通过「屏幕显示动态二维码 + 摄像头采集」的光学信道，在物理隔离（air-gapped）环境之间做**纯单向**数据摆渡：无网络、无 U 盘、无蓝牙 / Wi-Fi，数据只穿过摄像头镜头。

核心设计是 **LT 喷泉码 + 无反馈单向链路**——发送端持续广播喷泉符号，接收端收齐略多于 K 个“任意”符号即可恢复，**丢帧无需反向信道请求重传，也无需操作员手动补帧**。PC 与 Android 双端均既能发送也能接收，且两端协议经字节级黄金参考（fixtures）交叉对齐，保证异构互通。

## 当前特性

**协议与编码**
- 动态 QR 帧流：`MANIFEST → DATA* → END`，周期重发 MANIFEST。
- **LT 喷泉码前向纠错**：系统化发送（先直发 K 个源块，再无限生成 RSD 随机组合符号），漏帧无需等下一轮循环到特定块，消除顺序轮询的补帧长尾。
- 帧级 CRC32、文件级 SHA-256 双重校验；接收完成按 SHA-256 验证原始数据。
- 单向链路友好：接收端无需回传协商，可容忍乱序、重复和部分丢帧。
- 内容类型：文本与文件；自适应压缩分片（当前 `NONE` / `ZLIB`，`ZSTD` 预留）。

**QR / 彩色码后端**
- 标准 QR（默认）：`segno` 编码、`zxing-cpp` 解码，二进制安全、单帧多码识别。
- QFC1 彩色矩阵码：四色矩阵 + 颜色标定 + 分块 Reed-Solomon 纠错，**受控环境实验模式**，非默认链路。

**PC 端（Python / PySide6）**
- 发送 / 接收双页 GUI，OpenCV 摄像头采集，FPS / 纠错级别 / 网格可调。
- 缺块可视化：实时展示未恢复的源块索引（缺 0 块隐藏，超 20 个截断）。
- 人工补发：发送端游标驱动的可追加帧源，操作员可从当前 symbol 续传补缺。
- 断点续传：发送端 / 接收端均可持久化进度并中断恢复。
- 链路统计：实时有效 / 丢弃帧数、丢弃率、耗时，完成时显示吞吐量。

**Android 端（Kotlin / CameraX + ML Kit）**
- **接收**：CameraX 预览 + 1080p ImageAnalysis，ML Kit 扫码驱动 `ReceivePipeline`；文件接收完成后由用户通过系统文件选择器自选保存路径与文件名。
- **发送**：文本与文件均可发送；标准 QR（推荐）/ 彩色码（实验）切换；8 / 12 / 15 FPS；帧生成与渲染在后台线程，主线程只贴图；发送时自动拉满屏幕亮度并保持唤醒。
- 链路诊断：状态栏实时显示识别 / 有效 / 丢弃计数、进度、缺块、耗时。
- `protocol` 纯 JVM 库与 PC 端字节级对齐，含独立 golden 测试套件。

**真机状态**：PC ↔ Android 真机链路已跑通，文本 / 图片（PNG/JPG）/ 文档（doc/docx）均完成端到端传输并通过 SHA-256 校验。

## 与同类项目对比

均为「屏幕 + 摄像头」光学离线传输，设计取舍不同：

| 维度 | **qr-ferry** | [AirScan-QR](https://github.com/topcss/AirScan-QR) | [libcimbar](https://github.com/sz3/libcimbar) | [airgap-qr-transfer-bar-code](https://github.com/liumu927/airgap-qr-transfer-bar-code) |
| --- | --- | --- | --- | --- |
| 技术栈 | Python(PySide6) + Kotlin | 纯 Web（单 HTML） | C++（WASM 编码 / Android 解码） | C++/Qt |
| 编码载体 | 标准 QR（+实验 QFC1） | 标准 QR | 彩色图标矩阵码（高密度） | 标准 QR（+实验 Cimbar） |
| 抗丢机制 | **LT 喷泉码，纯单向** | 数组索引 + 手动点击补帧 | wirehair 喷泉码 + RS | **feedback QR 触发重发**（需反向信道） |
| 单向纯度 | ✅ 无需任何反馈 | ✅（补帧靠人工） | ✅ | ❌ 重发依赖反馈 |
| 跨语言协议一致 | ✅ fixtures 字节级对照 | 单实现 | 单实现 | 单实现 |
| 双端收发 | PC 收发 + Android 收发 | 浏览器收发 | Web 发 + Android 收 | Win 收发 + Android 收发 |

**与 AirScan-QR**：同为轻量 QR 流传输。AirScan-QR 零安装、浏览器即用、无痕；qr-ferry 是原生应用，换取更高的渲染 / 解码性能与更严谨的协议（CRC32 + SHA-256 + 会话状态机）。补帧思路上，AirScan-QR 依赖操作员在缺失矩阵上点击单点重传，qr-ferry 由 LT 喷泉码自动消化丢帧。

**与 libcimbar**：libcimbar 用彩色图标矩阵码达成高密度、高吞吐（约 850 kbps / 106 KB/s），是单帧信息密度的上限探索；qr-ferry 选用兼容性最好的标准 QR，单帧密度更低、吞吐不及，但任何主流 QR 库 / 扫描器均可解析，实现与移植门槛低。纠错上 libcimbar 用 Reed-Solomon + 专利的 wirehair 喷泉码，qr-ferry 用经典无专利的 LT 码。

**与 airgap-qr-transfer-bar-code**：二者思路同源——`manifest → data → end`、CRC32 / SHA-256、air-gapped、双端。airgap-qr-transfer 是同一作者的 C++/Qt 实现，接收端用「反馈 QR」把缺块清单回传发送端触发重发；qr-ferry 是用 Python / Kotlin 重新实现的演进版本，**以 LT 喷泉码替代反馈重发，把链路收敛为真正无需反向信道的纯单向摆渡**，并补齐了跨语言协议一致性 fixtures。前者还探索了固定式扫描器（USB CDC）与 Cimbar，定位偏工程原型；后者聚焦协议清晰度与可读性。

**qr-ferry 的取舍小结**
- 优势：纯单向无反馈、双端收发、跨语言协议对齐、经典无专利 LT、Python 易读易扩展。
- 取舍：标准 QR 密度低于彩色码，吞吐不及 libcimbar；非 Web 工具，需安装；彩色码 QFC1 仍为实验模式。

## 架构

### PC 端（`src/qrferry/`）

```text
core/
├── crc32.py        # CRC32 封装
├── frame.py        # 协议帧与 payload 编解码
├── chunker.py      # 定长分块与重组
├── lt.py           # LT 喷泉码编码 / peeling 解码（RSD/ISD/DEGENERATE）
└── session.py      # 接收会话状态机
qr/
├── backend.py        # CodecBackend 抽象与标准 QR 实现（segno / zxing-cpp）
├── reed_solomon.py   # 分块 RS 纠错（彩色码用）
└── color_matrix.py   # QFC1 彩色矩阵码（实验）
app/
├── send_controller.py    # 发送端纯逻辑流水线
├── receive_pipeline.py   # 接收端纯逻辑流水线
├── session_store.py      # 断点续传持久化
├── camera_devices.py     # 摄像头枚举
└── main_window.py        # PySide6 GUI 与摄像头 / 渲染绑定
```

分层边界：`core` 不依赖第三方库，可复用于 CLI / 移动端协议对齐；`qr` 只负责字节 ↔ 图像转换，不绑定具体 QR 库；`app` 负责业务编排与 GUI，发送 / 接收核心逻辑保持可单测。

### Android 端（`android/`）

```text
protocol/（纯 JVM 库，与 PC 字节级对齐）
├── core/
│   ├── Proto.kt / Crc32.kt        # 常量与 CRC32
│   ├── FrameCodec.kt              # 帧解码（+ FrameEncoder.kt 编码）
│   ├── LtDecoder.kt / LtEncoder.kt# LT 喷泉码编解码
│   └── ReceiveSession.kt          # 接收会话状态机
└── test/.../fixture/Fixtures.kt   # 与 PC fixtures 逐字节对照的 golden 测试
app/
├── MainActivity.kt                # 发送 / 接收双模 UI、CameraX + ML Kit 绑定
├── pipeline/ReceivePipeline.kt    # 接收流水线（对齐 receive_pipeline.py）
└── send/
    ├── TextSendController.kt      # 发送端文本 / 文件流水线
    ├── QrRenderer.kt              # 标准 QR 渲染
    └── ColorMatrixRenderer.kt     # QFC1 彩色码渲染
```

## 协议概览

- 帧序列：`MANIFEST → DATA* → END`，MANIFEST 周期重发以便接收端随时加入。
- LT 喷泉码（协议 §6）：系统化发送——前 K 个符号 `degree=1` 直发源块（无丢包时一轮即完成），之后按 RSD 度分布无限生成随机组合符号；接收端收齐略多于 K 个“任意”符号即可 peeling 解码恢复。`degree / adjacency` 在 DATA 帧中显式传输，解码端无需 PRNG 协商。
- 校验：每帧 CRC32，整文件 SHA-256；坏帧（CRC 失败 / MAGIC 不符 / 解析异常）静默丢弃，绝不崩溃。
- 压缩：当前 `NONE` / `ZLIB`；`ZSTD` 预留。
- 跨语言一致：`tests/fixtures/protocol/protocol_v1.json` 为字节级黄金参考，Android 端 `Fixtures.kt` 逐字段对照，保证 PC ↔ Android 互通。

## 快速开始

### PC 端

```powershell
cd E:\project\qr-ferry
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
.venv\Scripts\python.exe scripts\run_app.py
```

- **发送页**：选择文件或输入文本，设置 FPS / 纠错级别 / 网格，开始发送后窗口持续渲染 QR 帧流。
- **接收页**：选择摄像头索引（30 FPS 上限自动采集），开始接收后将摄像头对准发送端 QR；文本完成显示在结果框，文件完成后点「保存文件」自选路径。

### Android 端

```bash
cd android
./gradlew :app:assembleDebug --no-daemon      # 调试 APK
./gradlew :app:assembleRelease --no-daemon    # 签名 release APK（需 keystore）
```

- 安装后授予相机权限，进入接收页对准 PC 发送端 QR；或切到发送页选择文本 / 文件后开始广播。
- 模式切换、编码（标准 QR / 彩色码）、FPS（8 / 12 / 15）均在界面内选择。

> 本机若系统 `JAVA_HOME` 默认为 JDK 8，构建需显式指定 JDK 17（Gradle 8.7 / AGP 8.5 要求）。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\roundtrip_selftest.py
```

- `tests/` 覆盖 core（CRC32 / 帧 / 分块 / LT / 会话）、qr（标准 QR / RS / 彩色码后端）、app（发送 / 接收流水线 / GUI / 摄像头 / 续传）与协议 fixtures 共 15 个测试模块。
- `roundtrip_selftest.py`：文本、文档、二进制、大文件端到端 SHA-256 往返校验。
- Android `protocol` 模块：`Crc32GoldenTest` / `FrameCodecTest` / `LtDecoderTest` / `LtEncoderTest` / `ReceiveSessionTest`，与 PC fixtures 逐字节对照。

## 依赖说明（PC 端）

- `PySide6==6.8.3`：锁定 6.8 LTS，规避部分 anaconda 派生 venv 的 Qt DLL 加载问题。
- `segno`：QR 编码，二进制安全。
- `zxing-cpp`：QR 解码，支持单图多码识别。
- `opencv-python`：摄像头采集与图像预处理。

## 已知限制与路线图

- 真实光学链路的系统性测试矩阵（屏幕亮度 / 距离 / 角度 / 分辨率 / FPS 组合）仍需扩充，以量化吞吐与完成率。
- 标准 QR 单帧密度有限，大文件吞吐低于彩色码方案；QFC1 彩色码仍为实验模式，未作为默认链路。
- Android 发送端文件流已打通，真机收发矩阵（Android→PC、Android→Android）需进一步累积。
- GUI 参数偏工程验证，尚缺基于统计的调参向导。

## 相关文档

- `docs/android-implementation-plan.md`：Android 端实施蓝图。
- `tests/fixtures/protocol/protocol_v1.json`：协议 v1 字节级黄金参考（MANIFEST / DATA / END 帧、LT 符号、SHA-256），PC 与 Android 共用，保证跨语言一致。
