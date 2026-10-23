"""QR 编解码适配层 —— 协议 v1.0 §10。

CodecBackend 抽象接口；StandardQrBackend 用 qrcode + Pillow 编码、zxing-cpp 解码。
未来彩色矩阵码实现同一接口（v2+ 插槽），上层无感知切换。
编码强制字节模式（MODE_8BIT_BYTE），保证协议二进制帧的 round-trip 安全。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from PIL import Image

__all__ = ["CodecBackend", "StandardQrBackend"]


class CodecBackend:
    """QR 编解码后端抽象接口。"""

    def encode(self, data: bytes, ecc_level: str = "Q") -> Image.Image:
        raise NotImplementedError

    def decode(self, image) -> list[bytes]:
        """从一张图像解码出若干段字节（支持多码网格）。"""
        raise NotImplementedError


class StandardQrBackend(CodecBackend):
    """标准 QR 后端：qrcode 编码 + zxing-cpp 解码。"""

    _ECC: ClassVar[dict[str, str]] = {"L": "l", "M": "m", "Q": "q", "H": "h"}   # segno 用小写

    def __init__(self) -> None:
        # 最近一次成功解码的几何信息 (码宽px, 图像宽px, 该码字节数)。
        # UI 用它把「码占画面多大」换算成「每个模块几个像素」，从而判断该让用户
        # 靠近取景（免费提速）还是降低帧长（牺牲吞吐）。解不出码时保持上次的值。
        self.last_code_geometry: tuple[float, int, int] | None = None
        # 三级回退（放大+锐化）的自限流计数：尝试次数与命中次数。
        self._enhanced_attempts = 0
        self._enhanced_hits = 0

    _ENHANCED_PROBE_FRAMES = 40   # 至少试这么多次再判断值不值得继续

    def _enhanced_worth_trying(self) -> bool:
        """三级回退是否还值得跑：试够样本仍零命中就停用（省下每帧 ~15ms）。"""
        if self._enhanced_attempts < self._ENHANCED_PROBE_FRAMES:
            return True
        return self._enhanced_hits > 0

    def encode(self, data: bytes, ecc_level: str = "Q",
               box_size: int = 10, border: int = 4) -> Image.Image:
        from io import BytesIO

        import segno
        from PIL import Image

        qr = segno.make(data, error=self._ECC[ecc_level], mode="byte")
        buf = BytesIO()
        qr.save(buf, kind="png", scale=box_size, border=border)
        buf.seek(0)
        return Image.open(buf).convert("L")

    def decode(self, image) -> list[bytes]:
        import numpy as np
        import zxingcpp
        from PIL import Image

        if isinstance(image, Image.Image):
            image = np.array(image)
        # formats 限定标准 QR：协议只发 QR，不限定时 zxing 会把每帧对所有码制各扫一遍，
        # 实测 1280×720 单帧 13.4ms → 2.9ms（约 4.7 倍）；多码网格仍可一次返回多段。
        # try_rotate/try_invert/try_downscale 默认已开启。
        #
        # 二值化两级回退：LocalAverage 对屏幕拍摄的渐变反光最鲁棒，但当整屏偏暗或
        # 反光把局部对比压平时它会失效；此时再用 GlobalHistogram（全局直方图）重扫一次。
        # 只在第一级一无所获时多花 ~2ms，正常帧零额外成本。
        results = self._read(image, zxingcpp.Binarizer.LocalAverage)
        if not results:
            results = self._read(image, zxingcpp.Binarizer.GlobalHistogram)
        if not results and self._enhanced_worth_trying():
            # 三级回退：放大 + 锐化。见 _decode_enhanced。
            # 该级在 1280×720 上约 15ms（放大后像素翻 4 倍），而失败帧占比常达四成，
            # 无脑执行会白烧 40%×15ms 的解码预算。这里按命中率自我限流：连续多帧
            # 一无所获就停用（撕裂帧/失焦帧放大也救不回来，纯属浪费）。
            self._enhanced_attempts += 1
            results = self._decode_enhanced(image)
            if results:
                self._enhanced_hits += 1
        # 记录最近一次「找到码」时的几何尺寸：码宽 / 图像宽 → 供 UI 换算每个模块分到
        # 几个相机像素。这是判断「该靠近」还是「该降帧长」的唯一客观依据：
        # 同一取景下模块数越多，每模块像素越少，越过临界值就整帧扫不出。
        self._record_geometry(results, image)
        return [r.bytes for r in results if r.bytes]

    def _record_geometry(self, results, image) -> None:
        try:
            import numpy as np

            arr = np.asarray(image)
            img_w = int(arr.shape[1])
        except Exception:  # noqa: BLE001 — 诊断信息不能影响解码
            return
        best = 0.0
        for r in results:
            try:
                pos = r.position
                xs = (pos.top_left.x, pos.top_right.x, pos.bottom_right.x, pos.bottom_left.x)
                best = max(best, float(max(xs) - min(xs)))
            except Exception:  # noqa: BLE001, S110 — 个别结果缺位置信息，跳过该条
                pass
        if best > 0 and img_w > 0:
            # (码宽像素, 图像宽像素, 该码携带的字节数)
            nbytes = max((len(r.bytes) for r in results), default=0)
            self.last_code_geometry = (best, img_w, nbytes)

    def _decode_enhanced(self, image):
        """放大 2× + 锐化后的重试（密集码的兜底手段）。"""
        import cv2
        import numpy as np
        import zxingcpp

        # 解码入口允许三种图像类型（PIL / numpy / zxingcpp.Image），统一成单通道 uint8
        arr = np.asarray(image)
        if arr.ndim == 3:
            arr = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY) if arr.shape[2] >= 3 else arr[..., 0]
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        up = cv2.resize(arr, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        blurred = cv2.GaussianBlur(up, (0, 0), 1.2)
        sharp = cv2.addWeighted(up, 1.7, blurred, -0.7, 0)
        return self._read(sharp, zxingcpp.Binarizer.LocalAverage)

    @staticmethod
    def _read(image, binarizer):
        import zxingcpp

        return zxingcpp.read_barcodes(
            image,
            formats=zxingcpp.BarcodeFormat.QRCode,
            binarizer=binarizer,
        )
