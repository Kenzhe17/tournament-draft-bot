"""Shared game state across all mini-games."""

import logging

logger = logging.getLogger(__name__)


def clear_user_games(user_id: int) -> None:
    """Clear all active games for a user when thread is deleted."""
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
