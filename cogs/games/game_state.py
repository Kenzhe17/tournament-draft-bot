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
