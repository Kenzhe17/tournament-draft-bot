"""Redis client for caching and temporary storage."""

import json
from typing import Any, Optional

from config import REDIS_URL

_redis_client: Optional[Any] = None


async def get_redis():
    """Get or create Redis client."""
    global _redis_client
    if _redis_client is None:
        try:
            import redis.asyncio as redis
            _redis_client = await redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        except Exception:
            # If Redis is not available, return None
            return None
    return _redis_client


async def set_cache(key: str, value: Any, ttl: int = 300) -> bool:
    """Set a value in Redis cache with TTL in seconds."""
    redis = await get_redis()
    if not redis:
        return False

    try:
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        await redis.set(key, value, ex=ttl)
        return True
    except Exception:
        return False


async def get_cache(key: str) -> Optional[Any]:
    """Get a value from Redis cache."""
    redis = await get_redis()
    if not redis:
        return None

    try:
        value = await redis.get(key)
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return None
    except Exception:
        return None


async def delete_cache(key: str) -> bool:
    """Delete a value from Redis cache."""
    redis = await get_redis()
    if not redis:
        return False

    try:
        await redis.delete(key)
        return True
    except Exception:
        return False


async def set_leaderboard(guild_id: int, leaderboard_type: str, data: list, ttl: int = 60) -> bool:
    """Cache leaderboard data."""
    key = f"leaderboard:{guild_id}:{leaderboard_type}"
    return await set_cache(key, data, ttl)


async def get_leaderboard(guild_id: int, leaderboard_type: str) -> Optional[list]:
    """Get cached leaderboard data."""
    key = f"leaderboard:{guild_id}:{leaderboard_type}"
    return await get_cache(key)


async def set_minigame_session(session_id: str, data: dict, ttl: int = 300) -> bool:
    """Store mini-game session data."""
    key = f"minigame_session:{session_id}"
    return await set_cache(key, data, ttl)


async def get_minigame_session(session_id: str) -> Optional[dict]:
    """Get mini-game session data."""
    key = f"minigame_session:{session_id}"
    return await get_cache(key)


async def delete_minigame_session(session_id: str) -> bool:
    """Delete mini-game session data."""
    key = f"minigame_session:{session_id}"
    return await delete_cache(key)


async def set_cooldown(guild_id: int, user_id: int, command: str, ttl: int) -> bool:
    """Set a cooldown for a command."""
    key = f"cooldown:{guild_id}:{user_id}:{command}"
    return await set_cache(key, True, ttl)


async def get_cooldown(guild_id: int, user_id: int, command: str) -> bool:
    """Check if a command is on cooldown."""
    key = f"cooldown:{guild_id}:{user_id}:{command}"
    return await get_cache(key) is not None


async def close_redis() -> None:
    """Close Redis connection."""
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
