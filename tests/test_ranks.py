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


def test_custom_emoji_names_have_no_spaces(monkeypatch):
    """Discord custom emoji names не могут содержать пробелы:
    'Elite Heroic' → '<:Elite_Heroic:ID>'."""
    import config

    for i, tier in enumerate(ranks.RANK_TIERS):
        monkeypatch.setitem(config.RANK_EMOJIS, tier, str(1000 + i))
        tag = config.get_emoji(tier)
        assert tag.startswith("<:") and tag.endswith(f":{1000 + i}>")
        name = tag[2:].split(":")[0]
        assert " " not in name, (tier, tag)

    monkeypatch.setitem(config.RANK_EMOJIS, "Elite Heroic", "111")
    monkeypatch.setitem(config.RANK_EMOJIS, "Elite Master", "222")
    monkeypatch.setitem(config.RANK_EMOJIS, "Grandmaster", "333")
    assert config.get_emoji("Elite Heroic") == "<:Elite_Heroic:111>"
    assert config.get_emoji("Elite Master") == "<:Elite_Master:222>"
    assert config.get_emoji("Grandmaster") == "<:Grandmaster:333>"


def test_get_rank_emoji_uses_tier_and_full_name(monkeypatch):
    """get_rank_emoji: эмодзи тира + полное имя ступени."""
    import config
    from cogs.tournament import get_rank_emoji

    monkeypatch.setitem(config.RANK_EMOJIS, "Elite Heroic", "999")
    out = get_rank_emoji(70)  # Elite Heroic
    assert "<:Elite_Heroic:999>" in out
    assert "Elite Heroic" in out

    monkeypatch.setitem(config.RANK_EMOJIS, "Grandmaster", "888")
    out = get_rank_emoji(95)  # Grandmaster II
    assert "<:Grandmaster:888>" in out
    assert "Grandmaster II" in out


def _sample_profile_data(level: int) -> dict:
    return {
        "name": "Тест Игрок", "status": "проверка рангов",
        "level": level, "elo": 1523, "last_elo_change": 24,
        "skill_rating": 7.4,
        "matches": 128, "wins": 74, "winrate": 57.8, "kda": 1.9,
        "avg_kills": 4.2, "avg_damage": 1820, "max_kills": 12,
        "elo_history": [1400, 1420, 1450, 1440, 1480, 1523],
        "hist_start_elo": 1400, "chart_delta": 60,
        "balance": 12500, "items": 320, "reputation": 45,
        "partner": "—", "birthday": "—",
        "ult_matches": 12, "ult_wins": 7, "ult_elo": 1600,
        "ult_status": "В норме",
    }
