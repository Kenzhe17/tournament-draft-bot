"""Shared game state across all mini-games."""

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Track active threads per user (user_id -> thread_id)
# This is managed by the thread cog to enforce one thread per user
_active_threads: Dict[int, int] = {}


def get_active_threads() -> Dict[int, int]:
    """Get the active threads dictionary."""
    return _active_threads


def add_active_thread(user_id: int, thread_id: int) -> None:
    """Add an active thread for a user."""
    _active_threads[user_id] = thread_id


def remove_active_thread(user_id: int) -> None:
    """Remove an active thread for a user."""
    if user_id in _active_threads:
        del _active_threads[user_id]


def has_active_thread(user_id: int) -> bool:
    """Check if a user has an active thread."""
    return user_id in _active_threads


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
