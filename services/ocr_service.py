"""OCR-сервис на RapidOCR (ONNX Runtime) для скриншотов результатов FF.

Ленивая инициализация: ONNX-модели (~15 МБ) скачиваются при первом
запуске в ~/.rapidocr. В Docker они запекаются в образ (см. Dockerfile).

Возвращает список боксов: {"text", "x0", "y0", "x1", "y1", "cx", "cy"}.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_ocr_instance = None


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_instance = RapidOCR()
        logger.info("RapidOCR initialized")
    return _ocr_instance


def _box(text: str, x0: float, y0: float, x1: float, y1: float):
    return {
        "text": text.strip(),
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "cx": (x0 + x1) / 2,
        "cy": (y0 + y1) / 2,
    }


def _run_ocr(image_bytes: bytes) -> list[dict[str, Any]]:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    # RapidOCR ждёт BGR ndarray
    arr = np.asarray(img)[:, :, ::-1]
    result, _elapse = _get_ocr()(arr)

    boxes: list[dict[str, Any]] = []
    for item in result or []:
        try:
            poly, text = item[0], item[1]
        except (TypeError, IndexError):
            continue
        pts = np.asarray(poly, dtype=float).reshape(-1, 2)
        boxes.append(
            _box(
                str(text),
                float(pts[:, 0].min()),
                float(pts[:, 1].min()),
                float(pts[:, 0].max()),
                float(pts[:, 1].max()),
            )
        )
    return boxes


async def extract_text_boxes(image_bytes: bytes) -> list[dict[str, Any]]:
    """OCR изображения → список боксов с координатами."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_ocr, image_bytes)
