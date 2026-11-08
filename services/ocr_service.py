"""OCR-сервис на RapidOCR (ONNX Runtime) для скриншотов результатов FF.

Ленивая инициализация: ONNX-модели (~15 МБ) скачиваются при первом
запуске в ~/.rapidocr. В Docker они запекаются в образ (см. Dockerfile).

Возвращает список боксов: {"text", "x0", "y0", "x1", "y1", "cx", "cy"}.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import logging
import re
import threading
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_ocr_instance = None
_ocr_init_lock = threading.Lock()

# Отдельный движок rapidocr v3 с кириллическим rec — нужен только для
# названий команд на скриншоте («ХИЩНИКИ»/«РАЗРУШИТЕЛИ»): основной
# ch_PP-OCRv4_rec не знает кириллицу и выдаёт латиницу-кракозябры.
_cyr_instance = None
_cyr_init_lock = threading.Lock()

# Отдельный ограниченный пул для OCR: дефолтный executor даёт до ~32
# потоков — пачка скриншотов подряд запускает параллельные ONNX-инференсы
# и кладёт CPU/RAM. Два воркера = очередь вместо шторма.
OCR_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="ocr"
)

_KDA_RE = re.compile(r"\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{1,2}")


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_init_lock:
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
    return await loop.run_in_executor(OCR_EXECUTOR, _run_ocr, image_bytes)


def _get_cyrillic_ocr():
    global _cyr_instance
    if _cyr_instance is None:
        with _cyr_init_lock:
            if _cyr_instance is None:
                from rapidocr import (
                    EngineType,
                    LangRec,
                    ModelType,
                    OCRVersion,
                    RapidOCR,
                )

                _cyr_instance = RapidOCR(
                    params={
                        "Det.engine_type": EngineType.ONNXRUNTIME,
                        "Rec.engine_type": EngineType.ONNXRUNTIME,
                        "Cls.engine_type": EngineType.ONNXRUNTIME,
                        "Rec.lang_type": LangRec.CYRILLIC,
                        "Rec.model_type": ModelType.MOBILE,
                        "Rec.ocr_version": OCRVersion.PPOCRV5,
                    }
                )
                logger.info("RapidOCR cyrillic initialized")
    return _cyr_instance


def _run_ocr_cyrillic_region(
    image_bytes: bytes, region: tuple[float, float, float, float]
) -> str:
    """OCR области изображения кириллической моделью → текст или ''."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return ""
    x0, y0, x1, y1 = region
    pad = 14
    crop = img.crop(
        (
            max(0, int(x0) - pad),
            max(0, int(y0) - pad),
            min(img.width, int(x1) + pad),
            min(img.height, int(y1) + pad),
        )
    )
    if crop.width < 8 or crop.height < 8:
        return ""
    # Мелкий шрифт имени — увеличиваем, rec-модель любит ~48px высоту
    if crop.height < 64:
        scale = 64 / crop.height
        crop = crop.resize(
            (max(8, int(crop.width * scale)), 64), Image.LANCZOS
        )
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    arr = np.asarray(Image.open(buf).convert("RGB"))
    try:
        result = _get_cyrillic_ocr()(arr)
    except Exception:
        logger.exception("Cyrillic OCR failed")
        return ""
    txts = getattr(result, "txts", None) or ()
    text = " ".join(str(t).strip() for t in txts if str(t).strip())
    # Регистр: «хищники» → «Хищники» (первая буква каждого слова)
    words = []
    for w in text.split():
        c = w[0].lower() if w else ""
        if c == "ё" or "а" <= c <= "я":
            w = w[0].upper() + w[1:]
        words.append(w)
    return " ".join(words)[:48]


async def ocr_cyrillic_region(
    image_bytes: bytes, region: tuple[float, float, float, float]
) -> str:
    """Async-обёртка: кириллический OCR области названия команды."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        OCR_EXECUTOR, _run_ocr_cyrillic_region, image_bytes, region
    )


def ocr_score_fallback(
    image_bytes: bytes, boxes: list[dict[str, Any]]
) -> tuple[int, int] | None:
    """Повторный OCR полосы со счётом «N VS M», если основной проход
    не распознал цифры (мелкий/стилизованный шрифт).

    Вырезает полосу ±50px вокруг бокса VS, увеличивает x3 и прогоняет
    OCR заново. Возвращает (score_left, score_right) или None.
    """
    try:
        from services.cs_parser import VS_RE, parse_match_score
    except Exception:
        return None

    vs_boxes = [b for b in boxes if VS_RE.match(b["text"].strip())]
    if not vs_boxes:
        return None
    vb = min(vs_boxes, key=lambda b: b["cy"])

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None
    w, h = img.size
    y0 = max(0, int(vb["y0"]) - 50)
    y1 = min(h, int(vb["y1"]) + 50)
    if y1 - y0 < 20:
        return None
    crop = img.crop((0, y0, w, y1))
    crop = crop.resize((w * 3, (y1 - y0) * 3), Image.LANCZOS)

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    sub_boxes = _run_ocr(buf.getvalue()) or []
    return parse_match_score(sub_boxes)


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
    max_kda_y1 = max(b["y1"] for b in kda_boxes)

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None
    arr = np.asarray(img, dtype=np.int16)  # HxWx3
    h, w = arr.shape[:2]

    # Вертикальный шаг строк K/D/A — для нижней границы области панели
    row_cys = sorted(b["cy"] for b in kda_boxes)
    diffs = [row_cys[i + 1] - row_cys[i] for i in range(len(row_cys) - 1)]
    pitch = max(sorted(diffs)[len(diffs) // 2] if diffs else 90.0, 30.0)

    # Область панели: от шапки команд (~50% высоты первой строки K/D/A)
    # до низа таблицы. Берём ВЕСЬ регион, а не только полосу шапки —
    # подсветка победителя может быть рамкой/фоном по всей панели.
    y0 = int(min_kda_y * 0.50)
    y1 = int(max_kda_y1 + pitch * 1.1)
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
        maxc = np.maximum(np.maximum(r, g), b)
        minc = np.minimum(np.minimum(r, g), b)
        # Только насыщенные яркие пиксели (цветные элементы UI:
        # оранжевая подсветка победителя, синяя у проигравшего).
        # Белый/серый текст и тёмный фон исключаются — не размывают оценку.
        mask = (lum > 90) & ((maxc - minc) > 45)
        if not mask.any():
            return 0.0
        return float(np.mean((r - b)[mask]))

    margin = int(w * 0.03)
    left_score = _warmth(margin, int(boundary_x) - margin)
    right_score = _warmth(int(boundary_x) + margin, w - margin)

    diff = abs(left_score - right_score)
    # Победитель должен иметь реально тёплую (оранжево-золотую) подсветку
    winner_score = max(left_score, right_score)
    if diff < 40 or winner_score < 20:
        logger.info(
            "winner detect ambiguous: left=%.1f right=%.1f", left_score, right_score
        )
        return None
    side = "left" if left_score > right_score else "right"
    logger.info(
        "winner detect: %s (left=%.1f right=%.1f)", side, left_score, right_score
    )
    return side
