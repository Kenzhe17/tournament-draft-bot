"""OCR-сервис на PaddleOCR для скриншотов результатов Free Fire.

Ленивая инициализация: модели (~PP-OCRv5, lang='ru') скачиваются при
первом запуске в ~/.paddleocr. В Docker они запекаются в образ
(см. Dockerfile).

Возвращает список боксов: {"text", "x0", "y0", "x1", "y1", "cx", "cy"}.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_ocr_instance = None


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        from paddleocr import PaddleOCR

        _ocr_instance = PaddleOCR(
            lang="ru",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
        )
        logger.info("PaddleOCR initialized (lang=ru, cpu)")
    return _ocr_instance


def _normalize(results: Any) -> list[dict[str, float | str]]:
    """Приводит вывод PaddleOCR 3.x к плоскому списку боксов."""
    boxes: list[dict[str, float | str]] = []
    for page in results or []:
        # v3: объект с полями json/textures; v2: список [box, (text, score)]
        texts = getattr(page, "rec_texts", None) or (
            page.get("rec_texts") if isinstance(page, dict) else None
        )
        polys = getattr(page, "rec_polys", None) or (
            page.get("rec_polys") if isinstance(page, dict) else None
        )
        if texts is None:
            # fallback v2-style: [[box, (text, score)], ...]
            try:
                for item in page:
                    box, (text, _score) = item[0], item[1]
                    xs = [p[0] for p in box]
                    ys = [p[1] for p in box]
                    boxes.append(_box(text, min(xs), min(ys), max(xs), max(ys)))
            except Exception:
                continue
            continue
        for text, poly in zip(texts, polys or []):
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


async def extract_text_boxes(image_bytes: bytes) -> list[dict[str, Any]]:
    """OCR изображения → список боксов с координатами."""
    import asyncio

    def _run() -> list[dict[str, Any]]:
        img = Image.open(__import__("io").BytesIO(image_bytes)).convert("RGB")
        arr = np.asarray(img)
        ocr = _get_ocr()
        results = ocr.predict(arr)
        return _normalize(results)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)
