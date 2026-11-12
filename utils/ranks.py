"""Единая таблица рангов по уровню (1–100) + бейджи.

Полная лестница Free Fire CS с поддивизионами (27 ступеней):
Bronze/Silver I–III, Gold I–IV, Platinum/Diamond I–V,
Heroic, Elite Heroic, Master, Elite Master, Grandmaster I–II.
Границы уровней привязаны к прежним порогам тиров (1/13/26/39/51/63/76/88).
Каждой ступени соответствует PNG-эмблема FF CS из
assets_ds_server/ranked_badges/ (источник: FreeFire_Info_Web CS pack).
"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BADGE_DIR = BASE_DIR / "assets_ds_server" / "ranked_badges"

# Тиры рангов (для эмодзи и группировки) — от младшего к старшему
RANK_TIERS: tuple[str, ...] = (
    "Bronze", "Silver", "Gold", "Platinum", "Diamond",
    "Heroic", "Elite Heroic", "Master", "Elite Master", "Grandmaster",
)

# (название ступени, мин. уровень, файл бейджа)
RANKS: list[tuple[str, int, str]] = [
    # Bronze — уровни 1–12
    ("Bronze I", 1, "bronze_1.png"),
    ("Bronze II", 5, "bronze_2.png"),
    ("Bronze III", 9, "bronze_3.png"),
    # Silver — уровни 13–25
    ("Silver I", 13, "silver_1.png"),
    ("Silver II", 18, "silver_2.png"),
    ("Silver III", 22, "silver_3.png"),
    # Gold — уровни 26–38
    ("Gold I", 26, "gold_1.png"),
    ("Gold II", 30, "gold_2.png"),
    ("Gold III", 33, "gold_3.png"),
    ("Gold IV", 36, "gold_4.png"),
    # Platinum — уровни 39–50
    ("Platinum I", 39, "platinum_1.png"),
    ("Platinum II", 42, "platinum_2.png"),
    ("Platinum III", 44, "platinum_3.png"),
    ("Platinum IV", 47, "platinum_4.png"),
    ("Platinum V", 49, "platinum_5.png"),
    # Diamond — уровни 51–62
    ("Diamond I", 51, "diamond_1.png"),
    ("Diamond II", 54, "diamond_2.png"),
    ("Diamond III", 56, "diamond_3.png"),
    ("Diamond IV", 58, "diamond_4.png"),
    ("Diamond V", 60, "diamond_5.png"),
    # Heroic+ — уровни 63–100
    ("Heroic", 63, "heroic.png"),
    ("Elite Heroic", 69, "elite_heroic.png"),
    ("Master", 75, "master.png"),
    ("Elite Master", 81, "elite_master.png"),
    ("Grandmaster I", 88, "grandmaster_1.png"),
    ("Grandmaster II", 94, "grandmaster_2.png"),
]

# Эмодзи-фолбэки по тиру (кастомные — через config.RANK_EMOJIS)
RANK_EMOJI_FALLBACK = {
    "Bronze": "🥉",
    "Silver": "🥈",
    "Gold": "🥇",
    "Platinum": "💠",
    "Diamond": "🔷",
    "Heroic": "⚔️",
    "Elite Heroic": "🔥",
    "Master": "🎖️",
    "Elite Master": "🏅",
    "Grandmaster": "👑",
}


def rank_for_level(level: int) -> tuple[str, str]:
    """Вернуть (название ступени, имя файла бейджа) для уровня."""
    level = max(1, int(level or 1))
    name, _, badge = RANKS[0]
    for rank_name, min_level, badge_file in RANKS:
        if level >= min_level:
            name, badge = rank_name, badge_file
        else:
            break
    return name, badge


def rank_name(level: int) -> str:
    """Название ступени ранга, напр. «Platinum III»."""
    return rank_for_level(level)[0]


def rank_tier_name(level: int) -> str:
    """Тир ранга без поддивизиона, напр. «Platinum» — для эмодзи."""
    name = rank_name(level)
    for tier in sorted(RANK_TIERS, key=len, reverse=True):
        if name == tier or name.startswith(tier + " "):
            return tier
    return name


def rank_badge_path(level: int) -> Path | None:
    """Путь к PNG-эмблеме ступени (None если файла нет)."""
    _, badge = rank_for_level(level)
    path = BADGE_DIR / badge
    return path if path.exists() else None


def rank_badge_filename(level: int) -> str:
    return rank_for_level(level)[1]
