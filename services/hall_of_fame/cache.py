"""TTL-кэш результатов compute_all — один пересчёт на guild."""

from __future__ import annotations

import time

from services.hall_of_fame.records import compute_all

TTL_SECONDS = 120
_cache: dict[int, tuple[float, dict]] = {}


async def get_cached(guild_id: int):
    now = time.monotonic()
    hit = _cache.get(guild_id)
    if hit and now - hit[0] < TTL_SECONDS:
        return hit[1]
    data = await compute_all(guild_id)
    _cache[guild_id] = (now, data)
    return data


def invalidate(guild_id: int) -> None:
    _cache.pop(guild_id, None)
