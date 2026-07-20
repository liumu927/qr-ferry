# qr-ferry

> 光学二维码单向摆渡传输系统 · PC 端（Python 实现）

通过「屏幕显示二维码 + 摄像头采集」的光学信道，在物理隔离网络之间做单向数据摆渡。当前实现已经打通协议核心、标准 QR 编解码、PC 发送/接收 GUI、文本/文件落盘和自动化测试。

## 当前特性

- 动态 QR 帧流：`MANIFEST -> DATA* -> END` 循环播放。
- 协议核心：CRC32 帧校验、固定长度分块、LT 喷泉码前向纠错、会话状态机。
- 单向链路友好：接收端无需回传协商，可容忍乱序、重复和部分丢帧。
- 内容类型：支持文本与文件传输，文件按安全文件名落盘。
- 完整性校验：接收完成后按 SHA-256 校验原始数据。
- QR 后端：使用 `segno` 编码、`zxing-cpp` 解码，支持单码和 2x2 多码网格。
- PC GUI：PySide6 发送/接收双页、OpenCV 摄像头采集、进度显示、文本复制。
- 缺块可视化：接收页实时展示未恢复的源块索引，缺 0 块时隐藏，超 20 个截断显示。
- 人工补发：发送端游标驱动的可追加帧源，操作员可从当前 symbol 续传新符号补缺块。
- 断点续传：发送端/接收端均可持久化进度并中断恢复（接收端弹窗询问；发送端按 symbol_id 续传）。
- 链路统计：接收页实时展示有效/丢弃帧数、丢弃率、耗时，完成时显示吞吐量（KB/s）。
- 协议一致性 fixtures：固定 session_id 的字节级黄金参考（MANIFEST/DATA/END 帧 hex + LT 符号 + SHA-256），供 Android 等跨语言实现逐字节对照。

## 架构

```text
src/qrferry/
├── core/
│   ├── crc32.py       # CRC32 封装
│   ├── frame.py       # 协议帧与 payload 编解码
│   ├── chunker.py     # 定长分块与重组
│   ├── lt.py          # LT 喷泉码编码/peeling 解码
│   └── session.py     # 接收会话状态机
├── qr/
│   └── backend.py     # CodecBackend 抽象与标准 QR 实现
└── app/
    ├── send_controller.py    # 发送端纯逻辑流水线
    ├── receive_pipeline.py   # 接收端纯逻辑流水线
    └── main_window.py        # PySide6 GUI 与摄像头/渲染绑定
```

分层边界：

- `core` 不依赖第三方库，适合后续复用到 CLI、移动端协议对齐或更换 UI。
- `qr` 只负责字节与图像之间的转换，上层不绑定具体 QR 库。
- `app` 负责业务编排和 GUI，发送/接收核心逻辑仍保持可单测。

## 环境准备

```powershell
cd E:\project\qr-ferry
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 运行测试

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\roundtrip_selftest.py
```

当前验证结果：

- `pytest`: 99 passed
- `roundtrip_selftest.py`: 文本、文档、5KB 二进制、50KB 大文件均通过 SHA-256 校验

## 运行 GUI

```powershell
.venv\Scripts\python.exe scripts\run_app.py
```

发送页：

- 选择文件或输入文本。
- 设置 FPS、纠错级别和网格。
- 点击开始发送后，窗口持续渲染 QR 帧流。

接收页：

- 设置摄像头索引、保存目录和采集 FPS。
- 点击开始接收后，将摄像头对准发送端 QR。
- 文本完成后显示在结果框；文件完成后写入保存目录。

## 协议状态

传输协议规范 v1.0（字节级冻结）：

```text
参考文档：光学二维码摆渡传输系统-传输协议规范-v1.0.md
```

当前实现与协议相关的状态：

- 已实现：`MANIFEST`、`DATA`、`END`、CRC32、SHA-256、LT RSD/ISD/DEGENERATE。
- 已预留：`FEEDBACK`、`CIMBAR`、`Compression.ZSTD`。
- 当前可用压缩：`NONE`、`ZLIB`。

## 已知限制

- 尚未完成真实摄像头链路的系统性测试矩阵，例如不同屏幕亮度、距离、角度、分辨率和 FPS。
- Android 端尚未实现，跨语言协议一致性还需要单独测试。
- 彩色矩阵码和更高吞吐编码仍是后续方向，不属于当前稳定能力。
- GUI 参数仍偏工程验证，缺少吞吐量/误码率统计和真实链路调参向导。

## 依赖说明

- `PySide6==6.8.3`：锁定 6.8 LTS，规避部分 anaconda 派生 venv 中 Qt DLL 加载问题。
- `segno`：用于 QR 编码，二进制安全，避免 `python-qrcode` 在部分数据长度与纠错级组合上的稳定性问题。
- `zxing-cpp`：用于 QR 解码，支持单图多码识别。
- `opencv-python`：用于摄像头采集和图像预处理。

## 里程碑
- **M1** 协议核心层（已完成）：CRC32 / 帧编解码 / 分块 / LT 喷泉码 / 会话 / 端到端自测
- **M2** PC 双端打通（已完成）：OpenCV 采集 + ZXing 解码 + segno 渲染，PC↔PC 链路
- **M3** Android 端（fixtures 已做）：协议一致性黄金参考已生成；Kotlin + CameraX + ML Kit 本体待续
- **M4** 可靠性增强（已完成）：缺块可视化、人工补发、两端断点续传
- **M5** 加速项（部分）：2×2 多码网格、链路统计已做；彩色 QR、调参向导待续

## 下一步建议

M4 可靠性增强与 M5 链路统计已完成；M3 协议一致性 fixtures 已生成（跨语言对齐前置）。剩余风险在真实光学链路与 Android 本体。

优先级：

1. 建立真实 PC -> PC 测试矩阵（FPS/ECC/网格/距离/亮度），量化吞吐与完成率，验证人工补发与断点续传在真实丢帧下的有效性。
2. 基于 `tests/fixtures/protocol/protocol_v1.json` 在 Android Studio 中实现 Kotlin 接收端，逐字节对照协议（M3 本体）。
3. 补齐 M5 调参向导（基于统计的参数推荐）与彩色 QR。

