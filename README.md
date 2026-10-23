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
- 内容类型：文本与文件；自适应压缩分片（`NONE` / `ZLIB` / `ZSTD`，best-of-3 自动选择）；发送端可对 PNG/OOXML/PDF 做格式优化再编码。

**QR / 彩色码后端**
- 标准 QR（默认）：`segno` 编码、`zxing-cpp` 解码，二进制安全、单帧多码识别。
- QFC1 彩色矩阵码：四色矩阵 + 颜色标定 + 分块 Reed-Solomon 纠错，**受控环境实验模式**，非默认链路。

**PC 端（Python / PySide6）**
- 发送 / 接收双页 GUI，OpenCV 摄像头采集，FPS / 纠错级别 / 帧长 / 冗余 / 网格可调。
- 缺块可视化：实时展示未恢复的源块索引（缺 0 块隐藏，超 20 个截断）。
- 人工补发：发送端游标驱动的可追加帧源，操作员可从当前 symbol 续传补缺。
- 断点续传：发送端 / 接收端均可持久化进度并中断恢复。
- 链路统计：实时有效 / 丢弃帧数、丢弃率、耗时，完成时显示吞吐量。

**Android 端（Kotlin / CameraX + ML Kit）**
- **接收**：CameraX 预览 + 1080p ImageAnalysis，ML Kit 扫码驱动 `ReceivePipeline`；文件接收完成后由用户通过系统文件选择器自选保存路径与文件名。
- **发送**：文本与文件均可发送；当前固定标准 QR（彩色码实验分支已从界面撤下，底层保留）；FPS 5~30 多档可调；帧长 700 / 1200 / 2200 B（源块档位随帧长联动 512 / 1024 / 2048 B）；纠错 L / M；帧生成与渲染在后台线程，主线程只贴图；发送时自动拉满屏幕亮度并保持唤醒；文件选择后自动做无损优化（OOXML 重打包），「允许有损优化」默认关闭，勾选后照片 PNG 转 JPEG 等（接收文件会不同）。
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
├── compression.py        # 压缩编解码封装（zlib + zstd，自适应 best-of-3）
├── file_optimizer.py     # 文件格式优化（PNG/OOXML/PDF 再编码，可选）
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
- 压缩：`NONE` / `ZLIB` / `ZSTD`（zstd 级别 9，PC `zstandard` ↔ Android `zstd-jni` 互解）。发送端默认自适应取三者中压缩后最小者——文本/XML 自动 ZLIB/ZSTD，已压缩内容（PNG/DOCX/PDF）自动回退 NONE 直发。
- 文件优化（PC / Android 发送端，自动）：通用压缩对 PNG/DOCX/PDF 等已压缩容器无效，发送端自动做**无损**再编码——OOXML（docx/xlsx/pptx）zip 重打包、PDF 流重压缩（PC 端 pikepdf），接收端解包后内容与原件一致；**有损**转码（照片 PNG 转 JPEG、降位、OOXML 内嵌图片压缩）默认关闭，需显式勾选且会改变接收到的文件内容/格式（Android 端 PDF 优化暂不支持）。
- 跨语言一致：`tests/fixtures/protocol/protocol_v1.json` 为字节级黄金参考，Android 端 `Fixtures.kt` 逐字段对照，保证 PC ↔ Android 互通。

## 快速开始

### PC 端

> 免安装：从 [Release](https://github.com/liumu927/qr-ferry/releases) 下载 Windows zip，解压运行 `qr-ferry.exe`。以下为源码运行方式。

```powershell
cd E:\project\qr-ferry
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
.venv\Scripts\python.exe scripts\run_app.py
```

- **发送页**：选择文件或输入文本，设置 FPS / 纠错级别（默认 L）/ 帧长（700 / 1200 / 2200 B，源块档位随帧长联动）/ 冗余 / 网格，开始发送后窗口持续渲染 QR 帧流。
- **接收页**：摄像头列表自动探测可用设备（可手动刷新），开始接收后将摄像头对准发送端 QR；解码在后台线程进行，采集不阻塞。采集档位自动协商（1080p → 720p60 → 720p30，逐档回读校验，见「排障」一节）；文本完成显示在结果框，文件完成后点「保存文件」自选路径。

### Android 端

```bash
cd android
./gradlew :app:assembleDebug --no-daemon      # 调试 APK
./gradlew :app:assembleRelease --no-daemon    # 签名 release APK（需 keystore）
```

- 安装后授予相机权限，进入接收页对准 PC 发送端 QR；或切到发送页选择文本 / 文件后开始广播。
- 模式切换、FPS（5~30 多档）、帧长（700 / 1200 / 2200 B，默认 1200B）、纠错（L / M）均在界面内选择；码制当前固定标准 QR（彩色码未通过验证，已从界面撤下）。

> 本机若系统 `JAVA_HOME` 默认为 JDK 8，构建需显式指定 JDK 17（Gradle 8.7 / AGP 8.5 要求）。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\roundtrip_selftest.py
```

- `tests/` 覆盖 core（CRC32 / 帧 / 分块 / LT / 会话）、qr（标准 QR / RS / 彩色码后端）、app（发送 / 接收流水线 / GUI / 摄像头 / 续传）与协议 fixtures 共 15 个测试模块。
- `roundtrip_selftest.py`：文本、文档、二进制、大文件端到端 SHA-256 往返校验。
- `tools/qr_loopback_bench.py`：标准 QR 参数寻优基准，仿真"屏幕 → 相机"链路（时间混帧 / 模糊 / 噪声），比较帧长 × 纠错级 × 帧率 × 相机曝光组合的有效吞吐估计。
- Android `protocol` 模块：`Crc32GoldenTest` / `FrameCodecTest` / `LtDecoderTest` / `LtEncoderTest` / `ReceiveSessionTest`，与 PC fixtures 逐字节对照。

## 打包（Windows exe）

产物是 **onedir** 形态（`qr-ferry.exe` + 同目录 `_internal/`），不是单文件 exe——整包分发解压后双击运行。

```powershell
.venvScriptspyinstaller.exe --noconfirm --clean qr-ferry.spec
.venvScriptspython.exe scriptspack_windows_zip.py     # 可传 tag：pack_windows_zip.py v0.1.29
```

产出 `dist/qr-ferry-v<版本>-windows-x64.zip`（约 113 MB / 331 个文件）。

- **打包参数只在 `qr-ferry.spec` 里维护**：PyInstaller 命令、`segno`/`zxingcpp` 的 `collect_all`、以及排除掉的 25 个未用 Qt 模块（QtWebEngine / QtMultimedia / QtQuick …，这是体积从 300 MB+ 降到 113 MB 的原因）。本地与 CI 跑的是同一条命令，不要在别处另写一份 exclude 清单。
- **版本号三处必须一致**：`pyproject.toml`、`src/qrferry/__init__.py`（窗口/托盘自报的就是它）、git tag。`pack_windows_zip.py` 会校验，不一致直接拒绝产出，避免打出文件名与程序自报版本不符的错标包。
- **CI**：推送 `v*` tag 时 `release-pc` job 在 `windows-latest` 上自动打包并附到同一个 GitHub Release（与 Android APK 并列）。所以正常情况下**本地不需要打包**——云端产物直接进 Release，不会回到本地 `dist/`。
- `upx` 不在 PATH 时 spec 里的 `upx=True` 会静默退化为不压缩，不影响功能，装 UPX 可再显著缩小体积。

## 排障：识别不稳 / 速度上不去

拍屏链路的吞吐由**三个独立的量**决定，任何一个不满足都表现为「扫不出」或「很慢」。排障时按下面顺序看，不要一上来就降帧长。

### 1. 先看接收页的采集实况

「相机预览」标题右侧常驻一行实况，这是唯一可信的现场数据（不会被进度消息覆盖）：

```
1280×720 @ 60fps MJPG · 出帧 1280×720 · 解码 8.2ms · 每模块 4.1px/157模块
```

| 字段 | 含义 | 异常表现 |
|---|---|---|
| `1280×720 @ 60fps MJPG` | 协商后**实际生效**的参数 | 显示 `1280×720（协商 1920×1080 未生效）` 会标红 |
| `出帧 W×H` | 首帧真实尺寸 | 与协商值不符 = 驱动「报大出小」 |
| `解码 X ms` | 单帧解码耗时 | 持续 >20ms 说明失败帧在跑昂贵的回退 |
| `每模块 X.Xpx/N模块` | 实测码宽 ÷ 模块数 | <3px 才是真正的「像素不够」 |

`tools/diag_screen_scan.py` 可离线枚举设备的**全部**分辨率 / 编码组合与真实出帧率。

### 2. 三个瓶颈，按影响从大到小

**(a) 曝光时长 —— 最常见、也最容易被误判成「像素不够」**

手机屏幕换码是瞬时的，而相机每帧需要一段曝光时间。**曝光窗口一旦横跨换码瞬间，这一帧就是上半张旧码 + 下半张新码的撕裂帧**，任何解码器都救不回来。

撕裂占比可估算为 `曝光 / 换码周期`（单帧视角；实际吞吐还要乘以「每张码被采集多次」的补救收益）：

| 采集帧率 | 相机最大曝光 | 发送 12fps（周期 83ms） | 发送 24fps（周期 42ms） |
|---|---|---|---|
| 30fps | ~35ms | 约 42% 帧撕裂 | 约 **83%** 帧撕裂（几乎全废） |
| 60fps | ≤16.7ms | 约 20% | 约 40% |

关键结论：**采集帧率会物理性地封顶曝光时长**（`最大曝光 ≈ 1/帧率`）。因此把采集提到 60fps 不需要摄像头支持手动曝光，就能把撕裂减半——很多内置摄像头根本不支持 `CAP_PROP_EXPOSURE` 手动控制，这条路反而最稳。

发送帧率与有效吞吐并非单调关系。**经验近似**：设曝光 `e`、发送帧率 `f`，单位时间有效符号数 ≈ `f × (1 − e·f)`，最优在 `f* ≈ 1/(2e)`；曝光 35ms 时约 **14fps**，这正是「12fps 比 24fps 快一倍」的原因（实测 4.8 KB/s vs 2.6 KB/s）。

> 该式为一阶近似：它把「整帧干净才可解」当作二元条件，未计入「同一张码被采集多次、只要一次干净即可」的重复采样收益（真实模型是 `f × P(至少一帧干净)`）。因此它能正确排序「30fps 采集下 12fps 优于 24fps」，但不宜外推到 60fps 采集档——那里重复采样收益才显出来。精确值请以现场实况读数为准。

**(b) 每模块像素 —— 决定「能不能解」，但不决定「快不快」**

| 帧长 | 源块 | QR 版本 | 模块数 |
|---|---|---|---|
| 700B | 512B | v18 | 89 |
| 1200B | 1024B | v24 | 113 |
| 2200B | 2048B | v35 | 157 |

zxing 在干净帧上 3~4 px/模块 即可解出；低于 3px 才真的无解。**所以「像素够」不等于「快」，快慢主要看 (a)。**

**(c) 解码预算 —— 60fps 的前置条件**

失败帧要跑三级回退（LocalAverage → GlobalHistogram → 放大 2×+锐化），单帧可达 23ms。30fps 下占 69% 预算勉强够用；**60fps 下就是 138%，解码线程追不上会大量丢帧，表现为「提了帧率反而更慢」**。因此三级回退带命中率自限流：试满 40 帧仍零命中即永久停用，失败帧开销回落到约 8ms。

### 3. 实测参考（720p 内置摄像头 + 手机屏）

| 采集 | 发送 | 帧长 | 有效吞吐 | 备注 |
|---|---|---|---|---|
| 720p30 YUY2 | 12fps | 700B | 4.8 KB/s | 旧版默认，撕裂约 42% |
| 720p30 YUY2 | 24fps | 700B | **2.6 KB/s** | 曝光装不进 42ms 窗口，反向恶化 |
| 720p60 MJPG | 12fps | 1200B | **11 KB/s** | 接近理论值 12.3 KB/s（利用率 89%） |
| 720p60 MJPG | 12fps | 2200B | 可识别 | 理论值 24 KB/s，待压测 |

### 4. 常见误判

- ❌ 「扫不出就把帧长调低」——先用实况确认采集是否在 60fps。若还在 30fps，降帧长只是绕开症状。
- ❌ 「靠得越近越快」——取景放大只增加 px/模块，只影响「能否解」；已经能解时再靠近不会提速，说明瓶颈在 (a)。
- ❌ 「采集分辨率越高越好」——1080p30（曝光 33ms）未必优于 720p60（曝光 16.7ms），要先看哪个卡在曝光上。
- ❌ 「用仿真验证就够了」——干净合成图在 3.7px/模块 下 100% 可解，会得出「2200B 没问题」的错误结论；真实链路失效几乎都来自时间维度而非像素。

## 依赖说明（PC 端）

- `PySide6==6.8.3`：锁定 6.8 LTS，规避部分 anaconda 派生 venv 的 Qt DLL 加载问题。
- `segno`：QR 编码，二进制安全。
- `zxing-cpp`：QR 解码，支持单图多码识别。
- `opencv-python`：摄像头采集与图像预处理。

## 已知限制与路线图

- 真实光学链路的系统性测试矩阵（屏幕亮度 / 距离 / 角度 / 分辨率 / FPS 组合）仍需扩充，以量化吞吐与完成率。
- 标准 QR 单帧密度有限，大文件吞吐低于彩色码方案；QFC1 彩色码仍为实验模式，未作为默认链路。
- **拍屏吞吐的真正瓶颈是曝光而非像素**（见「排障」）：目前靠「60fps 采集强制短曝光」缓解，尚未实现多帧融合或反向光通道参数协商——前者可再救回部分撕裂帧，后者才能让发送端按接收端实际能力自动选择帧长/帧率。
- Android 发送端文件流已打通，真机收发矩阵（Android→PC、Android→Android）需进一步累积。
- GUI 参数偏工程验证，尚缺基于统计的调参向导。

## 相关文档

- `docs/android-implementation-plan.md`：Android 端实施蓝图。
- `tests/fixtures/protocol/protocol_v1.json`：协议 v1 字节级黄金参考（MANIFEST / DATA / END 帧、LT 符号、SHA-256），PC 与 Android 共用，保证跨语言一致。
