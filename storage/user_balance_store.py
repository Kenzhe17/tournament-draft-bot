"""User balance store for betting system."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

DATA_DIR = Path("data")
BALANCE_FILE = DATA_DIR / "user_balance.json"


class UserBalanceStore:
    """Store for managing user coin balances."""

    def __init__(self) -> None:
        self._use_db = True  # Использовать PostgreSQL
        self._balances: dict[str, int] = {}  # Key: "guild_id:user_id", Value: balance
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить балансы из файла."""
        if not BALANCE_FILE.exists():
            return

        try:
            with open(BALANCE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._balances = {k: int(v) for k, v in data.items()}
        except (json.JSONDecodeError, KeyError, ValueError):
            self._balances = {}

    def save(self) -> None:
        """Сохранить балансы в файл."""
        with open(BALANCE_FILE, "w", encoding="utf-8") as f:
            json.dump(self._balances, f, ensure_ascii=False, indent=2)

    def enable_db(self) -> None:
        """Enable database storage."""
        self._use_db = True

    async def get_balance(self, guild_id: int, user_id: int) -> int:
        """Get user balance."""
        key = f"{guild_id}:{user_id}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                if row:
                    return row["balance"]
                else:
                    # Create default balance
                    await conn.execute(
                        "INSERT INTO user_balance (guild_id, user_id, balance) VALUES ($1, $2, 100)",
                        guild_id, user_id
                    )
                    return 100
        else:
            if key not in self._balances:
                self._balances[key] = 100
            return self._balances[key]

    async def add_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        """Add coins to user balance. Returns new balance."""
        if amount < 0:
            raise ValueError("Amount must be positive")

        from config import MAX_BALANCE

        key = f"{guild_id}:{user_id}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Check current balance
                current = await self.get_balance(guild_id, user_id)
                if current + amount > MAX_BALANCE:
                    amount = MAX_BALANCE - current  # Cap at max balance

                await conn.execute(
                    """
                    INSERT INTO user_balance (guild_id, user_id, balance)
                    VALUES ($1, $2, 100)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET balance = user_balance.balance + $3
                    """,
                    guild_id, user_id, amount
                )
                row = await conn.fetchrow(
                    "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                return row["balance"] if row else 100
        else:
            if key not in self._balances:
                self._balances[key] = 100
            self._balances[key] = min(self._balances[key] + amount, MAX_BALANCE)
            self.save()
            return self._balances[key]

    async def subtract_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        """Subtract coins from user balance. Returns new balance."""
        if amount < 0:
            raise ValueError("Amount must be positive")

        current_balance = await self.get_balance(guild_id, user_id)
        if current_balance < amount:
            raise ValueError("Insufficient balance")

        key = f"{guild_id}:{user_id}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE user_balance SET balance = balance - $1 WHERE guild_id = $2 AND user_id = $3",
                    amount, guild_id, user_id
                )
                row = await conn.fetchrow(
                    "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                return row["balance"] if row else current_balance - amount
        else:
            self._balances[key] -= amount
            self.save()
            return self._balances[key]

    async def set_balance(self, guild_id: int, user_id: int, balance: int) -> int:
        """Set user balance to specific value."""
        if balance < 0:
            raise ValueError("Balance cannot be negative")

        key = f"{guild_id}:{user_id}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_balance (guild_id, user_id, balance)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET balance = $3
                    """,
                    guild_id, user_id, balance
                )
                return balance
        else:
            self._balances[key] = balance
            self.save()
            return balance


user_balance_store = UserBalanceStore()
