"""Shared game state across all mini-games."""

import logging
from datetime import datetime, date
from typing import Dict

logger = logging.getLogger(__name__)

# Track daily games with coin rewards per user
# Format: {guild_id: {user_id: {"date": "YYYY-MM-DD", "count": int}}}
_daily_games: Dict[int, Dict[int, Dict[str, any]]] = {}
DAILY_GAME_LIMIT = 5


def get_daily_games_count(guild_id: int, user_id: int) -> int:
    """Get the number of games played today with coin rewards."""
    today = date.today().isoformat()

    if guild_id not in _daily_games:
        _daily_games[guild_id] = {}

    if user_id not in _daily_games[guild_id]:
        _daily_games[guild_id][user_id] = {"date": today, "count": 0}

    user_data = _daily_games[guild_id][user_id]

    # Reset if it's a new day
    if user_data["date"] != today:
        user_data["date"] = today
        user_data["count"] = 0

    return user_data["count"]


def increment_daily_games(guild_id: int, user_id: int) -> int:
    """Increment daily games count and return new count."""
    today = date.today().isoformat()

    if guild_id not in _daily_games:
        _daily_games[guild_id] = {}

    if user_id not in _daily_games[guild_id]:
        _daily_games[guild_id][user_id] = {"date": today, "count": 0}

    user_data = _daily_games[guild_id][user_id]

    # Reset if it's a new day
    if user_data["date"] != today:
        user_data["date"] = today
        user_data["count"] = 0

    user_data["count"] += 1
    return user_data["count"]


def can_award_coins(guild_id: int, user_id: int) -> bool:
    """Check if user can still receive coin rewards today."""
    return get_daily_games_count(guild_id, user_id) < DAILY_GAME_LIMIT


def clear_user_games(user_id: int) -> None:
    """Clear all active games for a user when thread is deleted."""
    # Clear RPS games
    try:
        from cogs.games.rps import active_games, active_users, GameState
        # Remove user from active users
        if user_id in active_users:
            active_users.discard(user_id)
        # Mark all games where user is involved as ended
        games_to_remove = []
        for game_id, game in active_games.items():
            if game.initiator_id == user_id or game.opponent_id == user_id:
                game.state = GameState.END  # Mark as ended
                games_to_remove.append(game_id)
        for game_id in games_to_remove:
            if game_id in active_games:
                del active_games[game_id]
        logger.info(f"Cleared RPS games for user {user_id}")
    except ImportError:
        pass
    except Exception as e:
        logger.error(f"Error clearing RPS games: {e}")

    # Clear coin flip games
    try:
        from cogs.games.coin_flip import _user_locks
        # Remove user locks
        keys_to_remove = [k for k in _user_locks if k[1] == user_id]
        for key in keys_to_remove:
            del _user_locks[key]
        logger.info(f"Cleared coin flip locks for user {user_id}")
    except ImportError:
        pass
    except Exception as e:
        logger.error(f"Error clearing coin flip games: {e}")

    # Math quiz uses view-based state (no global tracking)
    # Views will timeout naturally when messages are deleted
    logger.info(f"Math quiz games for user {user_id} will be cleared with thread deletion")
