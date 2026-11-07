"""Парсер строки статистики игрока из модалок.

Форматы (разделитель "/"):
    kills/deaths                — например 8/2  (assists=0, damage=0)
    kills/deaths/assists        — например 8/2/1
    kills/deaths/assists/damage — например 8/2/1/2179
"""

from __future__ import annotations

MAX_KILLS = 99
MAX_DEATHS = 99
MAX_ASSISTS = 99
MAX_DAMAGE = 200000


def parse_kd_stat_value(text: str) -> dict[str, int] | None:
    """Разобрать строку статистики.

    Возвращает {"kills","deaths","assists","damage"} или None при ошибке.
    """
    if not text:
        return {"kills": 0, "deaths": 0, "assists": 0, "damage": 0}

    parts = [p.strip() for p in text.split("/")]
    if len(parts) < 2 or len(parts) > 4:
        return None
    if any(not p for p in parts):
        return None
    if not all(p.lstrip("-").isdigit() for p in parts):
        return None

    kills, deaths = int(parts[0]), int(parts[1])
    assists = int(parts[2]) if len(parts) > 2 else 0
    damage = int(parts[3]) if len(parts) > 3 else 0

    if not (0 <= kills <= MAX_KILLS and 0 <= deaths <= MAX_DEATHS):
        return None
    if not (0 <= assists <= MAX_ASSISTS and 0 <= damage <= MAX_DAMAGE):
        return None

    return {
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "damage": damage,
    }
