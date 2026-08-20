# UI 设计令牌（Design Tokens）

双端（PC PySide6 / Android Material3）统一的设计变量。改界面配色/圆角/间距时先改这里，
再同步两端实现，保证视觉一致。

## 色板

| 语义 | 浅色 | 深色 | 用途 |
| --- | --- | --- | --- |
| `bg` | `#F8FAFC` | `#0F172A` | 窗口/页面背景 |
| `card` / surface | `#FFFFFF` | `#1E293B` | 卡片表面 |
| `subcard` | `#F8FAFC` | `#0B1220` | 卡内次级分区（PC） |
| `ink` / onSurface | `#0F172A` | `#E2E8F0` | 主文字 |
| `muted` / onSurfaceVariant | `#64748B` | `#94A3B8` | 次要文字 |
| `line` / outline | `#E2E8EB` | `#334155` | 描边/分隔线 |
| `accent` / primary | `#2563EB` | `#3B82F6` | 主色（按钮/选中态） |
| `accent_hover` | `#1D4ED8` | `#60A5FA` | 主按钮 hover |
| `accent_pressed` | `#1E40AF` | `#2563EB` | 主按钮 pressed |
| `accent_soft` / primaryContainer | `#EFF6FF` | `#1E3A8A` | 浅底强调（徽标/tonal） |
| `ok` | `#16A34A` | `#4ADE80` | 成功 |
| `err` | `#DC2626` | `#F87171` | 错误/缺块 |
| `status_bg` | `#EE111111` | `#0B1220` | Android 状态卡底（保留） |
| `track` | `#E2E8EB` | `#334155` | 进度轨道/分段底 |

## 圆角

| 档位 | 值 | 用途 |
| --- | --- | --- |
| S | 8 px/dp | 小元素、下拉项 |
| M | 12 | 按钮、输入框 |
| L | 16 | 卡片、预览区 |
| XL | 20–24 | 主卡片、相机/QR 预览卡（PC 20、Android 18） |

## 间距

4pt 网格：`4 / 8 / 12 / 16 / 24 / 32`。页边距 20，卡片内边距 16–20，控件间 8–12。

## 字阶

| 层级 | 值 | 用途 |
| --- | --- | --- |
| 标题 | 15–22 bold | 页卡标题、品牌 |
| 正文 | 14 | 默认 |
| 辅助 | 12 | 统计、徽标、提示 |
| 等宽 | 12 monospace | Android 状态卡（保留诊断风格） |

## 实现位置

- PC：`src/qrferry/app/main_window.py` 的 `_build_style()`（QSS 变量）+ `_ICON_SVGS`（图标色）
- Android：`android/app/src/main/res/values/colors.xml`（浅色）、`values-night/colors.xml`（深色）、
  `values/themes.xml`（M3 语义色映射）
- 图标：PC 内嵌 SVG（`{color}` 占位随主题重建）；Android 用 M3 组件自带图标/文字
