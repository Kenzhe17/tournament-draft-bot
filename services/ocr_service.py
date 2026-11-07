"""OCR-сервис на RapidOCR (ONNX Runtime) для скриншотов результатов FF.

Ленивая инициализация: ONNX-модели (~15 МБ) скачиваются при первом
запуске в ~/.rapidocr. В Docker они запекаются в образ (см. Dockerfile).

Возвращает список боксов: {"text", "x0", "y0", "x1", "y1", "cx", "cy"}.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_ocr_instance = None

_KDA_RE = re.compile(r"\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{1,2}")


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


def detect_winner_side(
    image_bytes: bytes, boxes: list[dict[str, Any]]
) -> str | None:
    """Определяет победившую команду по цветовой подсветке панели.

    У победившей команды шапка панели подсвечена оранжево-золотым
    (avg R-B большой), у проигравшей — сине-серая. Возвращает
    "left"/"right" или None, если уверенности нет.
    """
    kda_boxes = [b for b in boxes if _KDA_RE.search(b["text"])]
    if len(kda_boxes) < 4:
        return None

    # Разбиваем K/D/A-боксы на две колонки по крупнейшему разрыву X
    kda_boxes.sort(key=lambda b: b["cx"])
    cxs = [b["cx"] for b in kda_boxes]
    gaps = [cxs[i + 1] - cxs[i] for i in range(len(cxs) - 1)]
    split_idx = max(range(len(gaps)), key=lambda i: gaps[i])
    if gaps[split_idx] < 80:
        return None
    left_boxes = kda_boxes[: split_idx + 1]
    right_boxes = kda_boxes[split_idx + 1 :]
    if not left_boxes or not right_boxes:
        return None

    boundary_x = (max(b["cx"] for b in left_boxes) + min(b["cx"] for b in right_boxes)) / 2
    min_kda_y = min(b["y0"] for b in kda_boxes)

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None
    arr = np.asarray(img, dtype=np.int16)  # HxWx3
    h, w = arr.shape[:2]

    # Полоса шапки команд: примерно от 55% до 98% высоты первой строки K/D/A
    y0 = int(min_kda_y * 0.55)
    y1 = int(min_kda_y * 0.98)
    y0 = max(0, min(y0, h - 2))
    y1 = max(y0 + 2, min(y1, h))

    def _warmth(x_start: int, x_end: int) -> float:
        region = arr[y0:y1, x_start:x_end]
        if region.size == 0:
            return 0.0
        r = region[:, :, 0].ravel()
        g = region[:, :, 1].ravel()
        b = region[:, :, 2].ravel()
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        mask = lum > 100
        if not mask.any():
            return 0.0
        # Средний "теплый" перекос: оранжевый/золотой фон vs синий/серый
        return float(np.mean((r - b)[mask]))

    margin = int(w * 0.03)
    left_score = _warmth(margin, int(boundary_x) - margin)
    right_score = _warmth(int(boundary_x) + margin, w - margin)

    diff = abs(left_score - right_score)
    if diff < 40:  # недостаточно уверенности
        logger.info(
            "winner detect ambiguous: left=%.1f right=%.1f", left_score, right_score
        )
        return None
    side = "left" if left_score > right_score else "right"
    logger.info(
        "winner detect: %s (left=%.1f right=%.1f)", side, left_score, right_score
    )
    return side
