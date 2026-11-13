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
import os
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from services import ocr_worker

logger = logging.getLogger(__name__)

_ocr_instance = None
_ocr_init_lock = threading.Lock()

# Отдельный движок rapidocr v3 с кириллическим rec — нужен только для
# названий команд на скриншоте («ХИЩНИКИ»/«РАЗРУШИТЕЛИ»): основной
# ch_PP-OCRv4_rec не знает кириллицу и выдаёт латиницу-кракозябры.
_cyr_instance = None
_cyr_init_lock = threading.Lock()

# Счётчик активных OCR-операций в пуле — для perf_metrics gauge.
_ACTIVE_OCR_OPS = 0


def active_ocr_ops() -> int:
    """Количество OCR-задач, сейчас выполняемых в OCR_EXECUTOR."""
    return _ACTIVE_OCR_OPS


def _tracked(fn):
    """Обёртка worker-функции: инкремент/декремент _ACTIVE_OCR_OPS."""

    def _w(*args, **kwargs):
        global _ACTIVE_OCR_OPS
        _ACTIVE_OCR_OPS += 1
        try:
            return fn(*args, **kwargs)
        finally:
            _ACTIVE_OCR_OPS -= 1

    return _w


class _CountingExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor, считающий активные задачи (gauge)."""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(_tracked(fn), *args, **kwargs)


# Отдельный ограниченный пул для OCR: дефолтный executor даёт до ~32
# потоков — пачка скриншотов подряд запускает параллельные ONNX-инференсы
# и кладёт CPU/RAM. Два воркера = очередь вместо шторма.
OCR_EXECUTOR = _CountingExecutor(
    max_workers=2, thread_name_prefix="ocr"
)

_KDA_RE = re.compile(r"\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{1,2}")


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_init_lock:
            if _ocr_instance is None:
                from rapidocr_onnxruntime import RapidOCR

                _ocr_instance = RapidOCR(
                    intra_op_num_threads=int(
                        os.environ.get("OCR_INTRA_OP_THREADS", "-1")
                    ),
                    inter_op_num_threads=int(
                        os.environ.get("OCR_INTER_OP_THREADS", "-1")
                    ),
                )
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


# Минимальная ширина для OCR: узкие скриншоты (телефон ~1280px)
# детектор RapidOCR читает хуже — апскейлим с сохранением пропорций,
# а координаты возвращаем в исходном пространстве.
_OCR_MIN_WIDTH = 1600
_OCR_MAX_UPSCALE = 2.5


def _run_ocr(image_bytes: bytes) -> list[dict[str, Any]]:
    """Полный OCR: worker-процесс, при сбое — локальный инференс."""
    if ocr_worker.worker_enabled():
        try:
            return ocr_worker.call("ocr_full", image_bytes) or []
        except ocr_worker.OcrWorkerError:
            logger.warning(
                "OCR worker unavailable — local fallback", exc_info=True
            )
    return _run_ocr_local(image_bytes)


def _run_ocr_local(image_bytes: bytes) -> list[dict[str, Any]]:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    scale = 1.0
    if img.width < _OCR_MIN_WIDTH:
        scale = min(_OCR_MAX_UPSCALE, _OCR_MIN_WIDTH / img.width)
        if scale > 1.0:
            img = img.resize(
                (int(img.width * scale), int(img.height * scale)),
                Image.LANCZOS,
            )
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
        if scale != 1.0:
            pts = pts / scale
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


def _cyr_region_text(img, region: tuple[float, float, float, float]) -> str:
    """OCR одной области уже декодированного изображения."""
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


def _run_ocr_cyrillic_region(
    image_bytes: bytes, region: tuple[float, float, float, float]
) -> str:
    """OCR области — worker, при сбое локально."""
    if ocr_worker.worker_enabled():
        try:
            return ocr_worker.call(
                "ocr_cyr_region", image_bytes, region
            ) or ""
        except ocr_worker.OcrWorkerError:
            logger.warning(
                "OCR worker unavailable — local fallback", exc_info=True
            )
    return _run_ocr_cyrillic_region_local(image_bytes, region)


def _run_ocr_cyrillic_region_local(
    image_bytes: bytes, region: tuple[float, float, float, float]
) -> str:
    """OCR области изображения кириллической моделью → текст или ''."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return ""
    return _cyr_region_text(img, region)


def _run_ocr_cyrillic_regions(
    image_bytes: bytes, regions: list[tuple[float, float, float, float]]
) -> list[str]:
    """OCR нескольких областей — worker, при сбое локально."""
    if ocr_worker.worker_enabled():
        try:
            res = ocr_worker.call(
                "ocr_cyr_regions", image_bytes, regions
            )
            if isinstance(res, list):
                return res
        except ocr_worker.OcrWorkerError:
            logger.warning(
                "OCR worker unavailable — local fallback", exc_info=True
            )
    return _run_ocr_cyrillic_regions_local(image_bytes, regions)


def _run_ocr_cyrillic_regions_local(
    image_bytes: bytes, regions: list[tuple[float, float, float, float]]
) -> list[str]:
    """OCR нескольких областей за одно декодирование изображения.

    Названия двух команд матча — два соседних region; раньше каждый
    вызов декодировал PNG заново (~10–25 МБ временного PIL-образа
    на регион). Здесь — одно декодирование на обе области.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return [""] * len(regions)
    return [_cyr_region_text(img, r) for r in regions]


async def ocr_cyrillic_region(
    image_bytes: bytes, region: tuple[float, float, float, float]
) -> str:
    """Async-обёртка: кириллический OCR области названия команды."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        OCR_EXECUTOR, _run_ocr_cyrillic_region, image_bytes, region
    )


async def ocr_cyrillic_regions(
    image_bytes: bytes, regions: list[tuple[float, float, float, float]]
) -> list[str]:
    """Async-обёртка: кириллический OCR нескольких областей."""
    if not regions:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        OCR_EXECUTOR, _run_ocr_cyrillic_regions, image_bytes, regions
    )


_MODEL_FILES = (
    # (метка пайплайна, пакет, относительный путь)
    # v1 движок (rapidocr_onnxruntime) — det/cls/rec, основной OCR
    ("v1-full", "rapidocr_onnxruntime", "models/ch_PP-OCRv4_det_infer.onnx"),
    ("v1-full", "rapidocr_onnxruntime", "models/ch_PP-OCRv4_rec_infer.onnx"),
    ("v1-full", "rapidocr_onnxruntime", "models/ch_ppocr_mobile_v2.0_cls_infer.onnx"),
    # v3 движок (rapidocr) — det/cls + cyrillic rec (названия команд).
    # PP-OCRv6_rec_small НЕ нужен: Rec.* перекрыт на PPOCRV5+
    # cyrillic+mobile в _get_cyrillic_ocr.
    ("v3-cyrillic", "rapidocr", "models/PP-OCRv6_det_small.onnx"),
    ("v3-cyrillic", "rapidocr", "models/ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
    ("v3-cyrillic", "rapidocr", "models/cyrillic_PP-OCRv5_rec_mobile.onnx"),
)


def verify_model_files() -> list[str]:
    """Проверить наличие всех ONNX-моделей в site-packages.

    RapidOCR при отсутствии файла молча скачивает его по сети —
    в проде это недопустимо в критическом пути матча. Возвращает
    список «метка: путь» отсутствующих файлов (пустой = всё на
    месте). Вызывается при старте (warmup) и в Dockerfile-сборке.
    """
    import importlib.util

    missing = []
    for label, pkg, rel in _MODEL_FILES:
        spec = importlib.util.find_spec(pkg)
        if spec is None or not spec.submodule_search_locations:
            missing.append(f"{label}: {pkg}:{rel} (package missing)")
            continue
        p = Path(list(spec.submodule_search_locations)[0]) / rel
        if not p.is_file():
            missing.append(f"{label}: {p}")
    return missing


def warmup() -> None:
    """Прогреть обе OCR-модели на пустом кадре (старт бота).

    Первый инференс RapidOCR стоит ~4 с (аллокация сессий ONNX) —
    лучше сделать его при запуске, а не на первом скриншоте матча.
    """
    missing = verify_model_files()
    if missing:
        logger.error(
            "OCR model files missing — RapidOCR will try network "
            "download at runtime: %s", "; ".join(missing)
        )
    if ocr_worker.worker_enabled():
        # Модели грузим в worker-процесс — основной процесс остаётся
        # без ONNX-сессий и их арен.
        ocr_worker.warmup()
        return
    import numpy as np

    dummy = np.zeros((64, 64, 3), dtype=np.uint8)
    try:
        _get_ocr()(dummy[:, :, ::-1])
    except Exception:
        logger.warning("OCR warmup failed", exc_info=True)
    try:
        _get_cyrillic_ocr()(dummy)
    except Exception:
        logger.warning("Cyrillic OCR warmup failed", exc_info=True)
    # Fast det-only движок — вторая ONNX-сессия; без прогрева первый
    # матч платил бы ~1–2с на её init.
    try:
        from services import fast_cs_ocr

        fast_cs_ocr._get_fast_det()
        fast_cs_ocr._detect_boxes(dummy[:, :, ::-1])
    except Exception:
        logger.warning("Fast det warmup failed", exc_info=True)


def _ocr_region_boxes(
    img: Image.Image,
    region: tuple[float, float, float, float],
    *,
    min_height: int = 48,
    max_upscale: float = 3.0,
) -> list[dict[str, Any]]:
    """OCR области уже декодированного изображения.

    Вырезает region (x0,y0,x1,y1), при необходимости увеличивает
    (мелкий шрифт строк таблицы), прогоняет полный RapidOCR и
    возвращает боксы с координатами в системе ИСХОДНОГО кадра.
    """
    x0, y0, x1, y1 = region
    pad = 6
    crop = img.crop(
        (
            max(0, int(x0) - pad),
            max(0, int(y0) - pad),
            min(img.width, int(x1) + pad),
            min(img.height, int(y1) + pad),
        )
    )
    if crop.width < 8 or crop.height < 8:
        return []
    scale = 1.0
    if crop.height < min_height:
        scale = min(max_upscale, min_height / crop.height)
    elif crop.width < 200:
        scale = min(max_upscale, 200 / crop.width)
    if scale > 1.0:
        crop = crop.resize(
            (max(8, int(crop.width * scale)), max(8, int(crop.height * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    sub = _run_ocr(buf.getvalue()) or []
    # Пересчёт координат: crop-offset + scale → исходное изображение
    ox = max(0, int(x0) - pad)
    oy = max(0, int(y0) - pad)
    out = []
    for b in sub:
        out.append(_box(
            b["text"],
            ox + b["x0"] / scale,
            oy + b["y0"] / scale,
            ox + b["x1"] / scale,
            oy + b["y1"] / scale,
        ))
    return out


def ocr_image_region(
    image_bytes: bytes,
    region: tuple[float, float, float, float],
    *,
    min_height: int = 48,
    max_upscale: float = 3.0,
) -> list[dict[str, Any]]:
    """Targeted OCR области изображения основным движком."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return []
    return _ocr_region_boxes(
        img, region, min_height=min_height, max_upscale=max_upscale
    )


def _run_ocr_regions(
    image_bytes: bytes,
    regions: list[tuple[float, float, float, float]],
) -> list[list[dict[str, Any]]]:
    """OCR нескольких областей за одно декодирование кадра.

    Раньше каждая область декодировала PNG заново (~10–25 МБ
    временного PIL-образа на вызов); refine-строк делает до 8
    таких вызовов на скриншот.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return [[] for _ in regions]
    return [_ocr_region_boxes(img, r) for r in regions]


async def ocr_image_regions_async(
    image_bytes: bytes,
    regions: list[tuple[float, float, float, float]],
) -> list[list[dict[str, Any]]]:
    """Async-обёртка: targeted OCR нескольких областей."""
    if not regions:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        OCR_EXECUTOR, _run_ocr_regions, image_bytes, regions
    )


async def ocr_image_region_async(
    image_bytes: bytes,
    region: tuple[float, float, float, float],
    **kwargs,
) -> list[dict[str, Any]]:
    """Async-обёртка targeted OCR области."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        OCR_EXECUTOR, ocr_image_region, image_bytes, region, **kwargs
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

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None
    w, h = img.size

    vs_boxes = [b for b in boxes if VS_RE.match(b["text"].strip())]
    if not vs_boxes:
        # «VS» не распознан — счёт может быть склеен («5:2») или
        # лежать в верхней полосе шапки. Кропаем верхние ~22% кадра.
        y0, y1 = 0, int(h * 0.22)
        if y1 - y0 < 20:
            return None
    else:
        vb = min(vs_boxes, key=lambda b: b["cy"])
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


def ordered_round_score(
    session: dict, team1_index: int, team2_index: int
) -> tuple[int, int] | None:
    """Счёт раундов, привязанный к командам (team1, team2).

    OCR «N VS M» на скриншоте всегда в порядке «левая панель : правая
    панель». Маппинг panel->team_index берём из session["maps"]
    (результат ручного OCR Mapping). Возвращает None, если счёт не
    распознан или стороны команд не установлены — вызывающий код
    обязан упасть на убийства/валидацию, а не угадывать порядок.

    Режим "rooms" (мультикомнатный матч): session["match_score"] —
    ручной ввод организатора [score_t1, score_t2]; иначе суммируем
    поскриншотные счета всех комнат (каждая комната — свой «N VS M»,
    привязанный к командам через её собственный maps). Если хоть в
    одной комнате счёт не распознан — None.
    """
    if session.get("mode") == "rooms":
        manual = session.get("match_score")
        if (
            isinstance(manual, (list, tuple)) and len(manual) == 2
        ):
            try:
                a, b = int(manual[0]), int(manual[1])
                if 0 <= a <= 99 and 0 <= b <= 99 and (a or b):
                    return (a, b)
            except (TypeError, ValueError):
                pass
        rooms = session.get("rooms") or []
        if not rooms:
            return None
        total_l = total_r = 0
        seen = False
        for room in rooms:
            sub = ordered_round_score(room, team1_index, team2_index)
            if sub is None:
                return None
            total_l += sub[0]
            total_r += sub[1]
            seen = True
        return (total_l, total_r) if seen else None

    raw = session.get("score")
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        s_left, s_right = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None

    panel_of: dict[int, str] = {}
    for key, mapping in (session.get("maps") or {}).items():
        panel = (mapping or {}).get("panel")
        if panel not in ("left", "right"):
            continue
        try:
            panel_of[int(key)] = panel
        except (TypeError, ValueError):
            continue

    p1 = panel_of.get(team1_index)
    p2 = panel_of.get(team2_index)
    if p1 == "left" and p2 == "right":
        return (s_left, s_right)
    if p1 == "right" and p2 == "left":
        return (s_right, s_left)
    return None
