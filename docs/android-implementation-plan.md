# qr-ferry Android 接收端实施蓝图（M3）

> 本文档是 Python PC 端 → Android 接收端的跨语言交接。目标：在 Android Studio 中实现一个能与 PC 发送端互通的接收端，**逐字节对齐协议 v1.0**。
>
> 权威对照源：`tests/fixtures/protocol/protocol_v1.json`（已入库）。任何协议实现都必须复现该文件的字节。

## 1. 范围与约束

- **只做接收端**。发送端仍由 PC（Python）承担；Android 是被动接收方。
- 单向光学链路：Android **无需回传**任何数据给发送端。
- 协议字节级冻结（v1.0）：实现必须复现 fixtures，不得"优化"或改协议。
- 当前 Python 端已实现：`MANIFEST/DATA/END`、CRC32、SHA-256、LT（RSD/ISD/DEGENERATE）、`Compression.NONE/ZLIB`。预留未实现：`FEEDBACK`、`CIMBAR`、`Compression.ZSTD`——Android 端**无需**实现这些。

## 2. 前置条件

- Android Studio（Hedgehog 或更新）+ Android SDK（API 26+，minSdk 26）。
- 模拟器或真机（真机更接近真实光学链路；模拟器需把 PC 屏幕的 QR 投到虚拟摄像头，较难，建议真机）。
- 摄像头权限（`android.permission.CAMERA`）。

## 3. 交接物（已就绪）

| 资产 | 路径 | 用途 |
|------|------|------|
| 黄金参考 fixtures | `tests/fixtures/protocol/protocol_v1.json` | 逐字节对照权威 |
| 生成脚本 | `scripts/gen_protocol_fixtures.py` | 扩展用例（更多 K / ISD / NONE 压缩） |
| 帧编解码参考 | `src/qrferry/core/frame.py` | Header/Payload 字节布局 |
| LT 编解码参考 | `src/qrferry/core/lt.py` | peeling decoder |
| 接收会话参考 | `src/qrferry/core/session.py` | 状态机 |
| CRC32 参考 | `src/qrferry/core/crc32.py` | = `java.util.zip.CRC32` |
| 接收流水线参考 | `src/qrferry/app/receive_pipeline.py` | 编排：解码→会话→落盘 |
| 端到端自测 | `scripts/roundtrip_selftest.py` | 协议层 round-trip 参考逻辑 |

## 4. 协议规范摘要（v1.0）

### 4.1 帧布局

```
| Header (18B) | Payload (变长, PAYLOAD_LEN) | CRC32 (4B, 小端) |
```

- 字节序：**全程小端**（little-endian）。
- CRC32 覆盖范围：`Header + Payload`（不含自身 4 字节）。

### 4.2 CRC32

- 变体：**IEEE 802.3**，与 `zlib.crc32` / ZIP CRC32 完全一致。
- 参数：多项式反射 `0xEDB88320`，初值 `0xFFFFFFFF`，输出异或 `0xFFFFFFFF`。
- **Android 实现**：直接用 `java.util.zip.CRC32`，不要自己造轮子：
  ```kotlin
  import java.util.zip.CRC32
  fun crc32(data: ByteArray): Int {
      val c = CRC32(); c.update(data); return c.value.toInt()
  }
  ```
- Python 参考见 `crc32.py`（查表实现，结果与上方等价）。

### 4.3 Header（18B，`<HBBIIIH`）

| 偏移 | 字段 | 类型 | 说明 |
|------|------|------|------|
| 0 | MAGIC | u16 | `0x4F51`（小端 `0x51 0x4F`），用于过滤摄像头误识别 |
| 2 | VERSION | u8 | `0x01` |
| 3 | TYPE | u8 | 1=MANIFEST, 2=DATA, 3=END, 4=FEEDBACK(预留), 5=CIMBAR(预留) |
| 4 | SESSION | u32 | 会话 ID |
| 8 | STREAM | u32 | 流 ID（当前固定 0） |
| 12 | SYMBOL | u32 | 符号 ID（**解码端不使用**，仅发送端确定性重放） |
| 16 | PAYLOAD_LEN | u16 | Payload 字节数 |

### 4.4 Payload

**MANIFEST**（`<BBBBI` + `<QQ>` + `<B>` + name + sha256）：
```
content_type    : u8    1=FILE, 2=TEXT
compression     : u8    0=NONE, 1=ZLIB
chunk_size_log  : u8    block_size = 1 << chunk_size_log
lt_dist         : u8    0=RSD, 1=ISD, 2=DEGENERATE
total_chunks    : u32   K（源块数）
raw_size        : u64   原始数据字节数
encoded_size    : u64   压缩后字节数（重组后按此截断）
name_len        : u8
filename        : name_len 字节，UTF-8（TEXT 传输时为空）
raw_sha256      : 32 字节
```

**DATA**（`<H` + `degree × <I>` + xor_data）：
```
degree          : u16
adjacency       : degree × u32，升序、去重的源块索引（0..K-1）
xor_data        : 剩余字节，长度 = block_size（= 1 << chunk_size_log）
```

**END**：仅 `raw_sha256`（32 字节）。

### 4.5 LT 喷泉码（解码）

- **解码端不需要 PRNG / 度分布（RSD/ISD）**。degree 和 adjacency 在 DATA 帧里显式传输，解码只做 peeling。
- `symbol_id` 解码端**不用**（仅发送端 `(session_id, symbol_id)` 确定性复现符号）。
- Peeling decoder 算法（参考 `lt.py:142`）：
  1. 维护 `resolved: Array<ByteArray?>(K)`，初始全 null。
  2. 每收一个符号 `(adjacency, xor_data)`：用已解块消减——遍历 adjacency，若 `resolved[i] != null`，则 `xor_data ^= resolved[i]` 并从 adjacency 移除。
  3. 消减后若剩 1 个未解邻接 → 解出该块，入 ripple；若剩多个 → 入 pending。
  4. Ripple 传播：每个新解出的块去消减所有 pending 符号，可能连锁解开。
  5. 完成：`resolved` 全非 null。
- 异或为**等长字节按位异或**（block_size 对齐）。

### 4.6 重组与校验

1. 取 `resolved` 的 K 个块拼接。
2. 截断到 `encoded_size`（最后一块可能不足 block_size，或 encoded 不是 block 整数倍）。
3. 解压：`compression==ZLIB` 用 `java.util.zip.Inflater`；`NONE` 直接用。
4. `SHA-256(重组数据) == manifest.raw_sha256` 校验。
5. FILE：落盘（文件名经 `safe_filename` 规整）；TEXT：解码 UTF-8 显示。

## 5. 模块划分与实现顺序

按依赖自底向上，每层都用 fixtures 做对照单测：

```
Layer 0  crc32, sha256, hex        ← java.util.zip 标准库
Layer 1  FrameCodec                ← Header/Payload 编解码 + CRC 校验
Layer 2  LtDecoder                 ← peeling decoder
Layer 3  ReceiveSession            ← MANIFEST→DATA→END 状态机 + 重组 + 校验
Layer 4  ReceivePipeline           ← 图像→多码识别→会话→落盘（编排）
Layer 5  CameraX UI                ← 摄像头采集 + 预览 + 进度/缺块/统计展示
```

**先做 Layer 1–3（纯 JVM 单测，对照 fixtures），再做 Layer 4–5（Android UI）**。协议层全部通过后再碰摄像头。

## 6. 逐模块对照策略

把 `protocol_v1.json` 放进 `app/src/test/resources/`，用 Kotlinx Serialization 解析。

### Layer 1 · FrameCodec
- 解析 `case.manifest_frame_hex`、`case.end_frame_hex`、`case.data_symbols[].frame_hex`（`hex` → `ByteArray`）。
- 断言：MAGIC、VERSION、TYPE、SESSION、PAYLOAD_LEN 正确；CRC32 自校验通过。
- 解码 MANIFEST payload，断言字段 = `case.session_id / K / raw_size / encoded_size / raw_sha256 / filename`。
- 解码 DATA payload，断言 `degree / adjacency / xor_data` == `data_symbols[i]`。

### Layer 2 · LtDecoder
- 对每个 case：把全部 `data_symbols` 的 `(adjacency, xor_data)` 喂入 LtDecoder。
- 断言 `is_complete == true`，且拼接截断后 == `bytesFromHex(case.input_hex)`（注：fixtures 的 input 是**原始未压缩**数据，故需先按 compression 解压 encoded 再比；或直接比对解压后 == input）。

### Layer 3 · ReceiveSession
- 按 `manifest_frame → data_symbols[*].frame → end_frame` 顺序 ingest。
- 断言完成、`reassemble()` 解压后 == `input_hex`、SHA-256 == `raw_sha256`。

### Layer 4 · ReceivePipeline
- 用 Python segno 把 fixtures 的帧编码成 QR PNG（PC 侧生成一批测试图），或 Android 测试用 zxing 编码。
- 把 QR 图喂入 pipeline，断言完成且结果 == input。这步验证"图像→解码→会话"全链路。

### Layer 5 · CameraX UI
- 真机对准 PC 发送端 GUI 显示的 QR，端到端联调。

## 7. 关键移植要点

1. **CRC32 用 `java.util.zip.CRC32`**——勿自实现（易错且无意义）。
2. **字节序全程小端**——`ByteBuffer.order(LITTLE_ENDIAN)` 或手动拼。
3. **LT 解码无需 PRNG/度分布**——degree/adjacency 显式传输，这是跨语言零成本对齐的关键。
4. **`symbol_id` 解码端忽略**——不要试图用它索引符号。
5. **ZLIB 解压**用 `java.util.zip.Inflater`（Python `zlib.compress` 默认 level 6，标准 zlib 流，Inflater 可解）。
6. **SHA-256** 用 `MessageDigest.getInstance("SHA-256")`。
7. **safe_filename**：basename、过滤 `<>:"/\|?*`、长度截断 200（参考 `receive_pipeline.py:28`）。
8. **多码网格（2×2）**：PC 端用 zxing-cpp 识别多码；Android 若用 ML Kit Barcode Scanning，确认其单帧多码识别能力，否则需 zxing-cpp 的 Kotlin/JNI 绑定。
9. **坏帧静默丢弃**：CRC 失败/MAGIC 不符/解析异常一律丢弃，绝不崩溃（协议 §11 安全模型）。

## 8. 依赖清单（build.gradle.kts）

```kotlin
// 协议层（纯 JVM，可单测）
implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")

// 摄像头
implementation("androidx.camera:camera-core:1.3.4")
implementation("androidx.camera:camera-camera2:1.3.4")
implementation("androidx.camera:camera-lifecycle:1.3.4")
implementation("androidx.camera:camera-view:1.3.4")

// QR 解码（二选一）
implementation("com.google.mlkit:barcode-scanning:17.3.0")   // ML Kit，需 Google Play 服务
// 或 zxing-cpp 的 Kotlin/JNI 绑定（多码识别更稳，与 PC 端同源）

// 测试
testImplementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")
testImplementation(kotlin("test"))
```

## 9. 构建与验证

```bash
# 1. JVM 单测（协议层，对照 fixtures）——必须在接 CameraX 之前全绿
./gradlew test

# 2. 扩展 fixtures（如需更多用例）
cd /path/to/qr-ferry && .venv/Scripts/python.exe scripts/gen_protocol_fixtures.py

# 3. Android instrumented test（CameraX + 合成 QR 图）
./gradlew connectedAndroidTest

# 4. 真机联调：PC 运行 scripts/run_app.py 发送，Android 接收
```

## 10. 已知陷阱

- **MAGIC 字节序**：`0x4F51` 小端存储是 `0x51 0x4F`，不要写反。
- **PAYLOAD_LEN 是 u16**：DATA 帧的 payload 含 degree×4 字节 adjacency，大 degree 时注意不超 QR 容量（PC 端限 degree ≤ 30，单帧 ≤ 1200B）。
- **encoded_size 截断**：重组的块拼接后**必须按 encoded_size 截断**，否则解压会失败（最后一块的填充字节不能送入解压器）。
- **adjacency 升序去重**：PC 端保证，解码端可信任；但若做防御性校验，发现非升序/越界应丢弃该帧。
- **重复符号幂等**：同一 `(adjacency, xor_data)` 二次喂入会被 peeling 自然忽略——PC 端靠此容忍重复帧。
- **重发旧 symbol_id 无增益**：喷泉码靠**新**符号补缺块；Android 端不关心 symbol_id，故无影响（仅供实现者理解协议）。

## 11. Python 参考清单（按实现顺序读）

| 顺序 | 文件 | 行号锚点 | 看什么 |
|------|------|----------|--------|
| 1 | `src/qrferry/core/crc32.py` | `calc` | CRC32 查表实现（= java CRC32） |
| 2 | `src/qrferry/core/frame.py` | `FrameHeader` / `ManifestPayload` / `DataPayload` / `EndPayload` / `encode_frame` / `decode_frame` | 字节级布局 |
| 3 | `src/qrferry/core/lt.py` | `LtDecoder.add_symbol` / `_resolve` / `_propagate` | peeling 算法 |
| 4 | `src/qrferry/core/session.py` | `ReceiveSession.ingest` / `reassemble` | 状态机 + 重组 |
| 5 | `src/qrferry/app/receive_pipeline.py` | `ReceivePipeline.process_image` / `_finalize` | 编排与落盘 |

---

**完成标准**：Android 接收端能对准 PC（`scripts/run_app.py`）发送的 QR，在 `protocol_v1.json` 三个用例（text_short / text_unicode / file_binary）上完整还原数据、SHA-256 校验通过。
