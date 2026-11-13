"""Интеграционный тест: batch-OCR vs последовательные вызовы.

Оптимизация сократила число декодирований PNG (1 вместо N),
не меняя регионы и движки. На реальном скриншоте проверяем,
что batch-результаты ИДЕНТИЧНЫ последовательным вызовам —
то есть «до и после оптимизации» выдача не изменилась.

Требует реальный файл в screens/ (репозиторий) или
OCR_BATCH_TEST_IMAGE=<путь>. Пропускается без изображения.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from services import ocr_service

REPO = Path(__file__).resolve().parents[1]
SCREENS = REPO / "screens"


def _find_image() -> Path | None:
    env = os.environ.get("OCR_BATCH_TEST_IMAGE", "").strip()
    if env:
        p = Path(env)
        return p if p.exists() else None
    # Полноэкранный скриншот матча Free Fire — самый «боевой» кейс.
    for name in (
        "Screenshot_20260622_014223_Free_Fire.jpg",
        "Screenshot_20260628_222638_Free_Fire_MAX.jpg",
        "IMG_7897.png",
        "image.png",
    ):
        p = SCREENS / name
        if p.exists():
            return p
    return None


_IMG = _find_image()
pytestmark = pytest.mark.skipif(
    _IMG is None, reason="реальный скриншот недоступен"
)


def _load_bytes() -> bytes:
    return _IMG.read_bytes()


def _rel_regions(w: int, h: int):
    """Регионы в пикселях: полосы, где обычно имена команд/счёт."""
    return [
        (int(w * 0.02), int(h * 0.05), int(w * 0.45), int(h * 0.16)),
        (int(w * 0.55), int(h * 0.05), int(w * 0.98), int(h * 0.16)),
        (int(w * 0.30), int(h * 0.30), int(w * 0.70), int(h * 0.45)),
        (int(w * 0.02), int(h * 0.55), int(w * 0.98), int(h * 0.75)),
    ]


class TestCyrillicBatchEquivalence:
    def test_batch_equals_sequential(self):
        """ocr_cyrillic_regions == N вызовов ocr_cyrillic_region."""
        data = _load_bytes()
        from PIL import Image
        import io

        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        regions = _rel_regions(w, h)

        batch = ocr_service._run_ocr_cyrillic_regions(data, regions)
        seq = [
            ocr_service._run_ocr_cyrillic_region(data, r)
            for r in regions
        ]
        assert batch == seq
        # Контроль, что тест не «пустой»: хотя бы один регион
        # с кириллическим текстом (шапка матча).
        assert any(t.strip() for t in batch), (
            "OCR не распознал ни одного региона — тест ничего "
            "не проверяет, проверьте координаты"
        )


class TestMainOcrBatchEquivalence:
    def test_batch_equals_sequential(self):
        """_run_ocr_regions == N вызовов ocr_image_region."""
        data = _load_bytes()
        from PIL import Image
        import io

        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        regions = _rel_regions(w, h)

        batch = ocr_service._run_ocr_regions(data, regions)
        seq = [
            ocr_service.ocr_image_region(data, r)
            for r in regions
        ]
        assert batch == seq
        # Контроль: основной движок нашёл хоть что-то.
        assert any(boxes for boxes in batch), (
            "OCR не распознал ни одного региона"
        )
