"""Smoke-тест Fast OCR на реальном скриншоте (опциональный).

Требует переменную окружения FAST_OCR_TEST_IMAGE=<путь к png> или
файл tests/fixtures/<basename>.png. Эталонная статистика —
tests/fixtures/ocr_reference.json. Тест пропускается, если
изображение недоступно (например, в CI без локальных файлов).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services import fast_cs_ocr

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _find_image(name: str) -> Path | None:
    env = os.environ.get("FAST_OCR_TEST_IMAGE", "").strip()
    if env:
        p = Path(env)
        return p if p.exists() else None
    stems = [name]
    if name.lower().endswith(".png"):
        stems += [name[:-4] + ext for ext in (".jpg", ".jpeg")]
    for stem in stems:
        for p in (FIXTURES / stem, Path.home() / "Downloads" / stem):
            if p.exists():
                return p
    return None


REFS = json.loads((FIXTURES / "ocr_reference.json").read_text("utf-8"))


def _players(parsed, key):
    return parsed.get(key) or []


def _stats(players):
    return [
        (p.get("kills"), p.get("deaths"), p.get("assists"), p.get("dmg"))
        for p in players
    ]


@pytest.mark.parametrize("name", sorted(REFS))
def test_real_screenshot_matches_reference(name):
    img = _find_image(name)
    if img is None:
        pytest.skip(f"реальный скриншот {name} недоступен")
    ref = REFS[name]
    from PIL import Image

    with Image.open(img) as im:
        if ref.get("size") and list(im.size) != ref["size"]:
            pytest.skip(
                f"{img.name} имеет размер {im.size}, "
                f"эталон ожидает {ref['size']} — другой скриншот"
            )
    parsed = fast_cs_ocr._extract_parsed(img.read_bytes())
    assert parsed is not None, "fast OCR не смог распарсить скриншот"

    assert parsed["score"] == ref.get("score", parsed["score"])
    assert len(_players(parsed, "team_a")) == 4
    assert len(_players(parsed, "team_b")) == 4

    for key in ("team_a", "team_b"):
        want = ref[key]
        got = _players(parsed, key)
        assert _stats(got) == _stats(want), (
            f"{key}: got {_stats(got)} want {_stats(want)}"
        )
