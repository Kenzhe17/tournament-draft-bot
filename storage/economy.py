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
    from storage.user_balance_store import user_balance_store
    
    # Use subtract_balance which checks balance internally
    try:
        await user_balance_store.subtract_balance(guild_id, user_id, amount)
        return True
    except ValueError:
        return False


async def release_escrow(user_id: int, guild_id: int, amount: int) -> None:
    """Return escrowed funds to user (refund).
    
    Args:
        user_id: Discord user ID
        guild_id: Discord guild ID
        amount: Amount to refund
    """
    from storage.user_balance_store import user_balance_store
    
    await user_balance_store.add_balance(guild_id, user_id, amount)


async def payout_winner(user_id: int, guild_id: int, amount: int) -> None:
    """Pay winnings to user (adds to current balance).
    
    Args:
        user_id: Discord user ID
        guild_id: Discord guild ID
        amount: Amount to add (already includes escrow if needed)
    """
    from storage.user_balance_store import user_balance_store
    
    await user_balance_store.add_balance(guild_id, user_id, amount)


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
