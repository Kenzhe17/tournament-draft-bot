"""Единая таблица рангов по уровню (1–100) + бейджи.

8 рангов без под-тиров; каждому уровню соответствует PNG-бейдж
из assets_ds_server/ranked_badges/.
"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BADGE_DIR = BASE_DIR / "assets_ds_server" / "ranked_badges"

# (название, мин. уровень, файл бейджа)
RANKS: list[tuple[str, int, str]] = [
    ("Bronze", 1, "bronze.png"),
    ("Silver", 13, "silver.png"),
    ("Gold", 26, "gold.png"),
    ("Platinum", 39, "platinum.png"),
    ("Diamond", 51, "diamond.png"),
    ("Master", 63, "master.png"),
    ("Elite", 76, "elite.png"),
    ("Legend", 88, "legend.png"),
]

# Стандартные эмодзи-фолбэки (кастомные — через config.RANK_EMOJIS)
RANK_EMOJI_FALLBACK = {
    "Bronze": "🥉",
    "Silver": "🥈",
    "Gold": "🥇",
    "Platinum": "💠",
    "Diamond": "🔷",
    "Elite": "⚡",
    "Master": "🎖️",
    "Legend": "👑",
}


def rank_for_level(level: int) -> tuple[str, str]:
    """Вернуть (название ранга, имя файла бейджа) для уровня."""
    level = max(1, int(level or 1))
    name, _, badge = RANKS[0]
    for rank_name, min_level, badge_file in RANKS:
        if level >= min_level:
            name, badge = rank_name, badge_file
        else:
            break
    return name, badge


def rank_name(level: int) -> str:
    return rank_for_level(level)[0]


def rank_badge_path(level: int) -> Path | None:
    """Путь к PNG-бейджу ранга (None если файла нет)."""
    _, badge = rank_for_level(level)
    path = BADGE_DIR / badge
    return path if path.exists() else None


def rank_badge_filename(level: int) -> str:
    return rank_for_level(level)[1]
