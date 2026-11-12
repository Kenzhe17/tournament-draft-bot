"""Тесты таблицы рангов utils/ranks.py (лестница FF CS с поддивизионами)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import ranks


def test_subdivision_names():
    assert ranks.rank_name(1) == "Bronze I"
    assert ranks.rank_name(5) == "Bronze II"
    assert ranks.rank_name(9) == "Bronze III"
    assert ranks.rank_name(13) == "Silver I"
    assert ranks.rank_name(44) == "Platinum III"
    assert ranks.rank_name(49) == "Platinum V"
    assert ranks.rank_name(51) == "Diamond I"
    assert ranks.rank_name(60) == "Diamond V"
    assert ranks.rank_name(63) == "Heroic"
    assert ranks.rank_name(69) == "Elite Heroic"
    assert ranks.rank_name(75) == "Master"
    assert ranks.rank_name(81) == "Elite Master"
    assert ranks.rank_name(88) == "Grandmaster I"
    assert ranks.rank_name(94) == "Grandmaster II"
    assert ranks.rank_name(100) == "Grandmaster II"


def test_boundaries_monotonic():
    """Пороги строго возрастают, уровень всегда даёт высший ранг."""
    mins = [min_lvl for _, min_lvl, _ in ranks.RANKS]
    assert mins == sorted(mins) and len(set(mins)) == len(mins)
    # Каждый уровень 1..100 резолвится в существующую ступень
    for level in (1, 12, 13, 25, 26, 38, 39, 50, 51, 62, 63, 68, 69,
                  74, 75, 80, 81, 87, 88, 93, 94, 100):
        name, badge = ranks.rank_for_level(level)
        assert name
        assert ranks.rank_badge_path(level) is not None, (level, badge)


def test_tier_names_for_emoji():
    assert ranks.rank_tier_name(44) == "Platinum"
    assert ranks.rank_tier_name(94) == "Grandmaster"
    assert ranks.rank_tier_name(69) == "Elite Heroic"
    assert ranks.rank_tier_name(75) == "Master"
    assert ranks.rank_tier_name(81) == "Elite Master"
    # Все тиры имеют fallback-эмодзи
    for tier in ranks.RANK_TIERS:
        assert tier in ranks.RANK_EMOJI_FALLBACK


def test_badge_files_exist_and_png():
    for _, _, badge in ranks.RANKS:
        path = ranks.BADGE_DIR / badge
        assert path.exists(), badge
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_progression_semantics():
    """Переход через границу дивизиона и тира."""
    assert ranks.rank_name(43) == "Platinum II"
    assert ranks.rank_name(44) == "Platinum III"  # новый дивизион
    assert ranks.rank_name(50) == "Platinum V"
    assert ranks.rank_name(51) == "Diamond I"     # новый тир
    assert ranks.rank_name(0) == "Bronze I"       # clamp
    assert ranks.rank_name(999) == "Grandmaster II"
