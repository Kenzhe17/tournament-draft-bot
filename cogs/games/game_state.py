"""Shared game state across all mini-games."""

import asyncio
import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)

# Track active threads per user (user_id -> thread_id)
# This is shared across all games to prevent starting new games when a thread is still open
_active_threads: Dict[int, int] = {}

# Track last thread creation time for global cooldown
_last_thread_creation: float = 0
_thread_creation_lock = asyncio.Lock()


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


async def wait_for_thread_cooldown() -> None:
    """Wait for global thread creation cooldown to prevent rate limits."""
    async with _thread_creation_lock:
        global _last_thread_creation
        current_time = time.time()
        time_since_last = current_time - _last_thread_creation

        logger.info(f"Thread cooldown check: time_since_last={time_since_last:.2f}s")

        # Wait at least 2 seconds between thread creations
        min_cooldown = 2.0
        if time_since_last < min_cooldown:
            wait_time = min_cooldown - time_since_last
            logger.info(f"Waiting for thread cooldown: {wait_time:.2f}s")
            await asyncio.sleep(wait_time)

        _last_thread_creation = time.time()
        logger.info(f"Thread cooldown passed, creation time updated")
