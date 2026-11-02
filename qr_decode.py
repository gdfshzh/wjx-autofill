#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二维码解码 —— 优先 zxing-cpp，回退 OpenCV。

为什么默认不用 OpenCV：解一张二维码要拖 42MB 的 opencv-python 加 12MB 的 numpy，
放进离线包里是最不划算的一块。zxing-cpp 不到 1MB、Pillow 7MB，识别率反而更好
（cv2.QRCodeDetector 对低分辨率截图和带噪点的照片经常认不出来）。

两个后端都没有时抛 QRDecodeError，消息里直接给出该装哪个包。
"""

import io
import os


class QRDecodeError(RuntimeError):
    pass


def _backend() -> str:
    """返回可用后端名：zxing / cv2 / 空串。"""
    try:
        import zxingcpp  # noqa: F401
        from PIL import Image  # noqa: F401
        return "zxing"
    except ImportError:
        pass
    try:
        import cv2  # noqa: F401
        return "cv2"
    except ImportError:
        return ""


def backend_name() -> str:
    return _backend() or "缺少解码库"


def _via_zxing(data: bytes) -> str:
    import zxingcpp
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    # 调色板图 / 带 alpha 的图 zxing 支持一般，统一转 RGB
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # 手机截图里的二维码常常只占一小块，放大后识别率明显提升
    for scale in (1, 2, 3):
        cur = img if scale == 1 else img.resize((img.width * scale, img.height * scale))
        try:
            results = zxingcpp.read_barcodes(cur)
        except Exception:
            continue
        for r in results:
            text = (getattr(r, "text", "") or "").strip()
            if text:
                return text
    return ""


def _via_cv2(data: bytes) -> str:
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise QRDecodeError("图片无法解析")
    det = cv2.QRCodeDetector()
    text, _, _ = det.detectAndDecode(img)
    if not text:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for scale in (2, 3, 4):
            big = cv2.resize(gray, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_CUBIC)
            text, _, _ = det.detectAndDecode(big)
            if text:
                break
    return text or ""


def decode_bytes(data: bytes) -> str:
    """从图片字节里解出二维码文本。找不到抛 QRDecodeError。"""
    if not data:
        raise QRDecodeError("空图片")

    be = _backend()
    if be == "zxing":
        text = _via_zxing(data)
        if text:
            return text
        # zxing 没解出来而机器上恰好也有 cv2，再给它一次机会
        try:
            import cv2  # noqa: F401
        except ImportError:
            raise QRDecodeError("未识别到二维码（换一张更清晰的图，或裁掉多余白边）")
        text = _via_cv2(data)
        if text:
            return text
        raise QRDecodeError("未识别到二维码（换一张更清晰的图，或裁掉多余白边）")

    if be == "cv2":
        text = _via_cv2(data)
        if text:
            return text
        raise QRDecodeError("未识别到二维码")

    raise QRDecodeError(
        "缺少二维码解码库，装一个即可：\n"
        "    python -m pip install zxing-cpp pillow\n"
        "或\n"
        "    python -m pip install opencv-python")


def decode_file(path: str) -> str:
    if not os.path.isfile(path):
        raise QRDecodeError(f"读不到图片: {path}")
    with open(path, "rb") as f:
        return decode_bytes(f.read())


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python qr_decode.py <图片路径>")
        raise SystemExit(2)
    print("后端:", backend_name())
    print("内容:", decode_file(sys.argv[1]))
