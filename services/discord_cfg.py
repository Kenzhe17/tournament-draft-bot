"""Runtime Discord resource IDs (Control Center → settings_service).

Единая точка чтения discord.* настроек с fallback на config-константы.
Все значения применяются к новым операциям без рестарта.
"""

from __future__ import annotations

from typing import Any


def _peek(guild_id: int | None, key: str, default: Any) -> Any:
    try:
        from services.settings_service import settings_service
        v = settings_service.peek(guild_id, key)
        return v if v is not None else default
    except Exception:  # noqa: BLE001
        return default


def channel_ids(guild_id: int | None, key: str,
                default: list[int]) -> list[int]:
    """JSON-список ID каналов → list[int], fallback на default."""
    v = _peek(guild_id, key, None)
    if isinstance(v, list) and v:
        try:
            return [int(x) for x in v]
        except (TypeError, ValueError):
            return default
    return list(default)


def channel_id(guild_id: int | None, key: str,
               default: int) -> int:
    v = _peek(guild_id, key, None)
    try:
        return int(v) if v else int(default)
    except (TypeError, ValueError):
        return int(default)


def role_ids(guild_id: int | None, key: str,
             default: list[int]) -> list[int]:
    return channel_ids(guild_id, key, default)
