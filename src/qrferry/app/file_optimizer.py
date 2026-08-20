"""发送端文件优化 —— 针对「已压缩容器」格式（PNG / OOXML / PDF）的再编码。

通用压缩（zlib/zstd）只对有冗余的数据有效；PNG / DOCX(XLSX/PPTX) / PDF 内部
已是压缩/编码后的容器，通用算法压不动。本模块按 magic bytes 分派格式专属优化器：

- PNG    照片 → JPEG（有损，5~20×）；截图 → 调色板量化（有损，2~5×）；无损重存兜底；
- OOXML  重打包（zip level 9）+ 内嵌图片再压缩（有损）+ 剥离缩略图；
- PDF    流重压缩（pikepdf，无损）——已压流多的 PDF 收益有限，未压流收益可观。

语义约定：优化只在「结果严格更小」时生效；发送端发送优化后字节，SHA-256 针对
发送字节计算，接收端校验天然一致。**有损变换会改变文件内容/格式（如 PNG→JPG），
必须由用户显式勾选「优化文件」后才会走此路径**，结果中的 lossy/description 供 UI 明示。
"""
from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass

from PIL import Image

__all__ = [
    "OptimizationResult",
    "optimize_bytes",
    "optimize_file",
    "sniff_type",
]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PDF_MAGIC = b"%PDF-"
OOXML_MARKER = "[Content_Types].xml"          # OOXML 容器标识（docx/xlsx/pptx）
_MEDIA_DIRS = ("word/media/", "xl/media/", "ppt/media/")
_MEDIA_EXTS = (".png", ".jpg", ".jpeg")


@dataclass
class OptimizationResult:
    data: bytes
    filename: str          # 可能改变扩展名（PNG→JPG）
    original_size: int
    optimized_size: int
    lossy: bool
    description: str       # 如「照片转 JPEG(q85): 2.1MB → 318KB」


def sniff_type(data: bytes) -> str | None:
    """按 magic bytes 识别可优化格式：png / ooxml / pdf / None。"""
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(PDF_MAGIC):
        return "pdf"
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        # zip 容器：是否 OOXML 由 _optimize_ooxml 内的 [Content_Types].xml 判定
        return "ooxml"
    return None


def optimize_bytes(data: bytes, filename: str = "", lossy: bool = True) -> OptimizationResult | None:
    """按内容类型优化；无可用优化（已最优/不识别/优化后更大）返回 None。"""
    if not data:
        return None
    kind = sniff_type(data)
    if kind == "png":
        return _optimize_png(data, filename, lossy)
    if kind == "ooxml":
        return _optimize_ooxml(data, filename, lossy)
    if kind == "pdf":
        return _optimize_pdf(data, filename)
    return None


def optimize_file(path: str, lossy: bool = True) -> OptimizationResult | None:
    """按文件路径优化（读入 → optimize_bytes）。文件不存在/读取失败返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return optimize_bytes(data, os.path.basename(path), lossy)


# ── PNG ───────────────────────────────────────────────────
def _optimize_png(data: bytes, filename: str, lossy: bool) -> OptimizationResult | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()   # 触发解码，损坏文件在此抛错
    except Exception:
        return None
    orig = len(data)
    best: OptimizationResult | None = None

    def _keep(candidate: OptimizationResult | None) -> None:
        nonlocal best
        if candidate is not None and (best is None or candidate.optimized_size < best.optimized_size):
            best = candidate

    # 候选 1：无损重存（Pillow optimize=True）
    try:
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        if buf.tell() < orig:
            _keep(_make_result(buf.getvalue(), filename, orig, False, "PNG 无损重存"))
    except Exception:
        pass

    if lossy:
        # 候选 2：调色板量化（截图/UI 类，视觉近无损）
        try:
            q = _quantize(img)
            if q is not None:
                buf = io.BytesIO()
                q.save(buf, "PNG", optimize=True)
                if buf.tell() < orig:
                    _keep(_make_result(buf.getvalue(), filename, orig, True, "PNG 调色板量化"))
        except Exception:
            pass
        # 候选 3：照片 → JPEG（有损、改扩展名；仅较大且无透明度的图像）
        try:
            rgb, alpha_used = _to_opaque_rgb(img)
            if rgb is not None and not alpha_used and (orig >= 200_000 or img.width * img.height >= 512 * 512):
                buf = io.BytesIO()
                rgb.save(buf, "JPEG", quality=85, optimize=True, progressive=True)
                if buf.tell() < orig:
                    jpg_name = os.path.splitext(filename)[0] + ".jpg"
                    _keep(_make_result(buf.getvalue(), jpg_name, orig, True, "照片转 JPEG(q85)"))
        except Exception:
            pass

    return best


def _quantize(img):
    """RGB/RGBA → 调色板（P 模式，≤256 色）；已是调色板或太小则跳过。"""
    if img.mode == "P":
        return None
    if img.width * img.height < 4096:
        return None
    return img.quantize(colors=256, method=Image.Quantize.FASTOCTREE)


def _to_opaque_rgb(img):
    """转不透明 RGB；返回 (RGB 图像, 是否原带透明)。RGBA 全透明像素视为可丢弃 alpha。"""
    if img.mode == "RGBA":
        alpha = img.getchannel("A")
        extrema = alpha.getextrema()
        if extrema != (255, 255):   # 存在半透明/全透明像素：保留 PNG（避免白底穿帮）
            return None, True
        return img.convert("RGB"), True
    if img.mode in ("RGB", "L"):
        return img.convert("RGB"), False
    return None, True


# ── OOXML（docx/xlsx/pptx）───────────────────────────────
def _optimize_ooxml(data: bytes, filename: str, lossy: bool) -> OptimizationResult | None:
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
        names = zin.namelist()
        if OOXML_MARKER not in names:
            return None
        entries = {}
        media_reduced = False
        for info in zin.infolist():
            payload = zin.read(info.filename)
            if lossy and _is_media(info.filename):
                new = _recompress_media(payload)
                if new is not None and len(new) < len(payload):
                    payload = new
                    media_reduced = True
            entries[info.filename] = (info, payload)
        # 剥离缩略图（冗余缓存，无损语义）
        entries.pop("docProps/thumbnail.jpeg", None)
        entries.pop("docProps/thumbnail.png", None)
    except (zipfile.BadZipFile, OSError):
        return None

    orig = len(data)
    out = io.BytesIO()
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for name, (info, payload) in entries.items():
                zi = zipfile.ZipInfo(name, date_time=info.date_time)
                zi.external_attr = info.external_attr
                zi.compress_type = zipfile.ZIP_DEFLATED   # writestr 默认沿用 STORED，须显式指定
                z.writestr(zi, payload)
    except (OSError, ValueError):
        return None
    if out.tell() >= orig:
        return None
    desc = "OOXML 重打包(zip9)" + (" + 压缩内嵌图片" if media_reduced else "")
    return _make_result(out.getvalue(), filename, orig, media_reduced, desc)


def _is_media(name: str) -> bool:
    return name.startswith(_MEDIA_DIRS) and name.lower().endswith(_MEDIA_EXTS)


def _recompress_media(payload: bytes) -> bytes | None:
    """内嵌图片再压缩：JPEG 重编码 q82；PNG 量化。返回更小的字节或 None。"""
    try:
        img = Image.open(io.BytesIO(payload))
        fmt = (img.format or "").upper()
        if fmt == "JPEG":
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
            return buf.getvalue() if buf.tell() < len(payload) else None
        if fmt == "PNG":
            q = _quantize(img)
            if q is None:
                return None
            buf = io.BytesIO()
            q.save(buf, "PNG", optimize=True)
            return buf.getvalue() if buf.tell() < len(payload) else None
    except Exception:
        return None
    return None


# ── PDF ───────────────────────────────────────────────────
def _optimize_pdf(data: bytes, filename: str) -> OptimizationResult | None:
    try:
        import pikepdf
        pdf = pikepdf.open(io.BytesIO(data))
        out = io.BytesIO()
        try:
            pdf.save(out, compress_streams=True, recompress_flate=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.generate)
        except TypeError:   # 旧版 pikepdf 无 recompress_flate 参数
            out = io.BytesIO()
            pdf.save(out, compress_streams=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.generate)
    except Exception:
        return None
    if out.tell() < len(data):
        return _make_result(out.getvalue(), filename, len(data), False, "PDF 流重压缩")
    return None


# ── 公共 ──────────────────────────────────────────────────
def _make_result(data: bytes, filename: str, orig: int, lossy: bool, desc: str) -> OptimizationResult:
    return OptimizationResult(
        data=data,
        filename=filename,
        original_size=orig,
        optimized_size=len(data),
        lossy=lossy,
        description=f"{desc}: {_fmt(orig)} → {_fmt(len(data))}",
    )


def _fmt(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"
