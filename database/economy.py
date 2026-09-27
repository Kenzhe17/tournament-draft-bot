"""Atomic database helper functions for economy operations."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def check_balance(user_id: int, guild_id: int, amount: int) -> bool:
    """Check if user has sufficient balance for a bet.
    
    Args:
        user_id: Discord user ID
        guild_id: Discord guild ID
        amount: Amount to check
        
    Returns:
        True if balance >= amount, False otherwise
    """
    from storage.db import get_pool
    from storage.user_balance_store import user_balance_store
    
    balance = await user_balance_store.get_balance(guild_id, user_id)
    return balance >= amount


async def hold_escrow(user_id: int, guild_id: int, amount: int) -> bool:
    """Atomically deduct bet from user balance (escrow lock).
    
    Args:
        user_id: Discord user ID
        guild_id: Discord guild ID
        amount: Amount to hold in escrow
        
    Returns:
        True if successful, False if insufficient balance
    """
    from storage.db import get_pool
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE user_balance SET balance = balance - $1 WHERE guild_id = $2 AND user_id = $3 AND balance >= $1",
            amount, guild_id, user_id
        )
        return result == 1


async def release_escrow(user_id: int, guild_id: int, amount: int) -> None:
    """Return escrowed funds to user (refund).
    
    Args:
        user_id: Discord user ID
        guild_id: Discord guild ID
        amount: Amount to refund
    """
    from storage.db import get_pool
    from storage.user_balance_store import user_balance_store
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_balance SET balance = balance + $1 WHERE guild_id = $2 AND user_id = $3",
            amount, guild_id, user_id
        )


async def payout_winner(user_id: int, guild_id: int, amount: int) -> None:
    """Pay winnings to user (adds to current balance).
    
    Args:
        user_id: Discord user ID
        guild_id: Discord guild ID
        amount: Amount to add (already includes escrow if needed)
    """
    from storage.db import get_pool
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_balance SET balance = balance + $1 WHERE guild_id = $2 AND user_id = $3",
            amount, guild_id, user_id
        )


async def get_balance(user_id: int, guild_id: int) -> int:
    """Get current user balance.
    
    Args:
        user_id: Discord user ID
        guild_id: Discord guild ID
        
    Returns:
        Current balance
    """
    from storage.user_balance_store import user_balance_store
    return await user_balance_store.get_balance(guild_id, user_id)
