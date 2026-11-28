"""Конфигурация спонтанных событий 2.0 — чтение runtime-настроек.

Все значения идут через ``settings_service.peek`` (синхронный кэш,
PG → legacy → default) — Control Center меняет их без рестарта.
scope=both: переопределение на гильдию возможно, иначе global.

Читается на КАЖДОЕ подходящее сообщение — функции обязаны быть
дешёвыми и не ходить в БД.
"""

from __future__ import annotations

from typing import Any

from services.settings_service import settings_service

# Ключи реестра → поля возвращаемого dict.
_INT_KEYS = {
    "cooldown_minutes": "events.cooldown_minutes",
    "window_minutes": "events.window_minutes",
    "min_unique_users": "events.min_unique_users",
    "min_messages": "events.min_messages",
    "max_per_author": "events.max_per_author",
    "min_message_interval_seconds":
        "events.min_message_interval_seconds",
    "min_active_authors_60s": "events.min_active_authors_60s",
    "coins_min": "events.coins_min",
    "coins_max": "events.coins_max",
    "xp_min": "events.xp_min",
    "xp_max": "events.xp_max",
    "rep_min": "events.rep_min",
    "rep_max": "events.rep_max",
    "growing_burn_min_seconds": "events.growing_burn_min_seconds",
    "growing_burn_max_seconds": "events.growing_burn_max_seconds",
}

_JSON_KEYS = {
    "reward_weights": "events.reward_weights",
    "type_weights": "events.type_weights",
    "disabled_types": "events.disabled_types",
    "item_exclude_categories": "events.item_exclude_categories",
}


def _peek(guild_id: int | None, key: str, default: Any) -> Any:
    try:
        return settings_service.peek(guild_id, key)
    except Exception:
        return default


def event_settings(guild_id: int | None) -> dict[str, Any]:
    """Снимок всех настроек событий для гильдии (sync, кэш)."""
    out: dict[str, Any] = {}
    for field, key in _INT_KEYS.items():
        try:
            out[field] = int(_peek(guild_id, key, 0))
        except (TypeError, ValueError):
            continue
    for field, key in _JSON_KEYS.items():
        v = _peek(guild_id, key, None)
        out[field] = v if isinstance(v, (dict, list)) else None
    return out


def feature_enabled(guild_id: int | None) -> bool:
    """Мастер-флаг feature.coin_drop_enabled (PG → settings.json)."""
    try:
        return bool(settings_service.peek(
            guild_id, "feature.coin_drop_enabled"))
    except Exception:
        return False


def clamp_range(lo: int, hi: int) -> tuple[int, int]:
    """min/max из настроек с защитой от перевёрнутых границ."""
    return (min(lo, hi), max(lo, hi))
