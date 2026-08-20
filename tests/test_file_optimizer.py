"""文件优化器单元测试 —— PNG / OOXML / PDF 格式专属再编码。

优化器只应在「结果严格更小」时返回结果；有损路径（JPEG/量化）须正确标记 lossy。
"""
import io
import os
import zipfile

from PIL import Image

from qrferry.app.file_optimizer import optimize_bytes, optimize_file, sniff_type


# ── PNG ───────────────────────────────────────────────────
def _screenshot_png() -> bytes:
    """UI/截图类：平色区 + 抗锯齿渐变 → 调色板量化收益大。"""
    import random
    rng = random.Random(7)
    img = Image.new("RGB", (640, 400))
    px = img.load()
    for y in range(400):
        for x in range(640):
            if y < 300:
                px[x, y] = (240, 240, 240) if (x // 8) % 2 == 0 else (18, 120, 200)
            else:
                r = int(255 * x / 640)
                px[x, y] = (r, 120, 90)
            if rng.random() < 0.02:
                px[x, y] = (200, 200, 200)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _photo_png() -> bytes:
    """照片类：平滑渐变 + 高斯感光噪声 → JPEG 转换收益大（PNG 存不下噪点）。"""
    import random
    rng = random.Random(42)
    img = Image.new("RGB", (1200, 800))
    px = img.load()
    for y in range(800):
        for x in range(1200):
            r = min(255, max(0, int(255 * x / 1200 + rng.gauss(0, 25))))
            g = min(255, max(0, int(255 * y / 800 + rng.gauss(0, 25))))
            b = min(255, max(0, int((255 * x / 1200 + 255 * y / 800) / 2 + rng.gauss(0, 25))))
            px[x, y] = (r, g, b)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_sniff_type():
    assert sniff_type(_screenshot_png()) == "png"
    assert sniff_type(b"%PDF-1.7\n...") == "pdf"
    assert sniff_type(b"PK\x03\x04...") == "ooxml"
    assert sniff_type(b"\x00" * 16) is None
    assert sniff_type(b"") is None


def test_optimize_png_screenshot_quantizes():
    data = _screenshot_png()
    res = optimize_bytes(data, "shot.png", lossy=True)
    assert res is not None, "截图 PNG 应可优化"
    assert res.optimized_size < res.original_size
    assert res.data.startswith(b"\x89PNG")
    assert res.filename == "shot.png"   # 无损/量化不改扩展名
    # 优化结果仍是可解码的 PNG
    img = Image.open(io.BytesIO(res.data))
    assert img.size == (640, 400)


def test_optimize_png_photo_converts_to_jpeg():
    data = _photo_png()
    res = optimize_bytes(data, "photo.png", lossy=True)
    assert res is not None
    assert res.optimized_size < res.original_size
    assert res.filename == "photo.jpg", "照片 PNG 应转 JPEG 并改扩展名"
    assert res.lossy
    assert res.data.startswith(b"\xff\xd8")


def test_optimize_png_lossless_only_never_lossy():
    data = _screenshot_png()
    res = optimize_bytes(data, "shot.png", lossy=False)
    # 无损模式可能无法压过原图（Pillow 默认已优化），但不允许有损结果
    if res is not None:
        assert not res.lossy
        assert res.filename == "shot.png"


def test_optimize_png_photo_lossless_only_keeps_format():
    """无损模式（默认）：照片 PNG 不得被转成 JPEG——内容保真是传输契约。"""
    data = _photo_png()
    res = optimize_bytes(data, "photo.png", lossy=False)
    if res is not None:
        assert not res.lossy
        assert res.filename == "photo.png", "无损模式禁止改扩展名（转 JPEG）"
        assert res.data.startswith(b"\x89PNG"), "无损模式禁止输出 JPEG 字节"


def test_optimize_unknown_or_incompressible_returns_none():
    assert optimize_bytes(os.urandom(4096), "rand.bin") is None
    assert optimize_bytes(b"") is None
    # 已是 JPEG：不在优化范围（magic 不识别）
    jpg = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(jpg, "JPEG", quality=85)
    assert optimize_bytes(jpg.getvalue(), "a.jpg") is None


# ── OOXML ─────────────────────────────────────────────────
def _make_docx(media: bytes | None = None, media_name: str = "word/media/image1.png") -> bytes:
    """合成最小 docx：未压缩存储 XML + 可选内嵌图片。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml",
                   ("<w:document xmlns:w='urn:doc'><w:body>%s</w:body></w:document>"
                    % ("<w:p>qr-ferry 光学摆渡 " * 50 + "</w:p>")).encode("utf-8"))
        if media is not None:
            z.writestr(media_name, media)
    return buf.getvalue()


def test_optimize_ooxml_repacks_stored_entries():
    data = _make_docx()
    res = optimize_bytes(data, "报告.docx", lossy=False)
    assert res is not None, "ZIP_STORED 的 docx 重打包应显著变小"
    assert res.optimized_size < res.original_size
    assert not res.lossy
    # 重打包产物仍是合法 zip，且 [Content_Types].xml 保留
    with zipfile.ZipFile(io.BytesIO(res.data)) as z:
        assert "[Content_Types].xml" in z.namelist()
        assert "word/document.xml" in z.namelist()
        assert z.read("[Content_Types].xml").startswith(b"<?xml")


def test_optimize_ooxml_recompresses_embedded_png():
    media = _screenshot_png()
    data = _make_docx(media=media, media_name="word/media/image1.png")
    res = optimize_bytes(data, "带图.docx", lossy=True)
    assert res is not None
    assert res.lossy, "内嵌图片被压缩 → 有损标记"
    with zipfile.ZipFile(io.BytesIO(res.data)) as z:
        assert z.read("word/media/image1.png").startswith(b"\x89PNG")


def test_optimize_ooxml_rejects_plain_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("a.txt", "hello")
    assert optimize_bytes(buf.getvalue(), "a.zip") is None   # 非 OOXML 容器


# ── PDF ───────────────────────────────────────────────────
def _uncompressed_pdf() -> bytes:
    import pikepdf
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    # 大段重复文本作为未压缩内容流（save 默认 compress_streams=True，显式关掉以构造可优化输入）
    line = b"BT /F1 12 Tf 72 720 Td (qr-ferry optical ferry hello) Tj ET\n"
    page.Contents = pikepdf.Stream(pdf, line * 300)
    buf = io.BytesIO()
    pdf.save(buf, compress_streams=False)
    return buf.getvalue()


def test_optimize_pdf_recompresses_streams():
    data = _uncompressed_pdf()
    assert data.startswith(b"%PDF-")
    res = optimize_bytes(data, "report.pdf", lossy=True)
    assert res is not None, "未压缩流 PDF 重压缩应变小"
    assert res.optimized_size < res.original_size
    assert not res.lossy
    assert res.data.startswith(b"%PDF-")
    # 产物可被 pikepdf 正常打开
    import pikepdf
    pdf = pikepdf.open(io.BytesIO(res.data))
    assert len(pdf.pages) == 1


def test_optimize_pdf_bad_input_returns_none():
    assert optimize_bytes(b"%PDF-1.7 garbage not a pdf", "x.pdf") is None


# ── 文件级入口 ───────────────────────────────────────────
def test_optimize_file_missing_returns_none(tmp_path):
    assert optimize_file(str(tmp_path / "no-such.png")) is None
