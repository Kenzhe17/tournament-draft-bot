"""User balance store for betting system."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from config import DATA_DIR, MAX_BALANCE  # noqa: E402 — единый путь из config
from storage.json_atomic import atomic_write_json
BALANCE_FILE = DATA_DIR / "user_balance.json"
DECAY_FILE = DATA_DIR / "decay_tracking.json"
DECAY_THRESHOLD = 10000  # Balance above this gets decayed
DECAY_PERCENT = 0.05  # 5% decay per day


class DecayTracker:
    """Track daily decay for users."""

    def __init__(self) -> None:
        self._last_decay_date: str = ""
        self._processed_users: set[str] = set()  # "guild_id:user_id"
        self.load()

    def load(self) -> None:
        """Load decay tracking from file."""
        if not DECAY_FILE.exists():
            return

        try:
            with open(DECAY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._last_decay_date = data.get("last_decay_date", "")
                self._processed_users = set(data.get("processed_users", []))
        except (json.JSONDecodeError, KeyError):
            self._last_decay_date = ""
            self._processed_users = set()

    def save(self) -> None:
        """Save decay tracking to file."""
        data = {
            "last_decay_date": self._last_decay_date,
            "processed_users": list(self._processed_users)
        }
        atomic_write_json(DECAY_FILE, data)

    def should_apply_decay(self, guild_id: int, user_id: int) -> bool:
        """Check if decay should be applied for this user today."""
        today = date.today().isoformat()
        key = f"{guild_id}:{user_id}"

        # If it's a new day, reset processed users
        if self._last_decay_date != today:
            self._last_decay_date = today
            self._processed_users = set()
            self.save()

        return key not in self._processed_users

    def mark_processed(self, guild_id: int, user_id: int) -> None:
        """Mark user as processed for today's decay."""
        key = f"{guild_id}:{user_id}"
        self._processed_users.add(key)
        self.save()


decay_tracker = DecayTracker()


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
        atomic_write_json(BALANCE_FILE, self._balances)

    def enable_db(self) -> None:
        """Enable database storage."""
        self._use_db = True

    @staticmethod
    async def _ensure_row(conn, guild_id: int, user_id: int) -> None:
        """Гарантировать существование строки баланса (дефолт 100)."""
        await conn.execute(
            """
            INSERT INTO user_balance (guild_id, user_id, balance)
            VALUES ($1, $2, 100)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            """,
            guild_id, user_id
        )

    @staticmethod
    async def _apply_decay(conn, guild_id: int, user_id: int, balance: int) -> int:
        """Применить дневной decay к балансу внутри транзакции.

        Возвращает актуальный баланс; при decay UPDATE уже выполнен.
        """
        if balance > DECAY_THRESHOLD and decay_tracker.should_apply_decay(guild_id, user_id):
            decay_amount = int(balance * DECAY_PERCENT)
            balance -= decay_amount
            await conn.execute(
                "UPDATE user_balance SET balance = $1 WHERE guild_id = $2 AND user_id = $3",
                balance, guild_id, user_id
            )
            decay_tracker.mark_processed(guild_id, user_id)
        return balance

    async def get_balance(self, guild_id: int, user_id: int) -> int:
        """Get user balance."""
        key = f"{guild_id}:{user_id}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await self._ensure_row(conn, guild_id, user_id)
                row = await conn.fetchrow(
                    "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
                return await self._apply_decay(conn, guild_id, user_id, row["balance"])
        else:
            if key not in self._balances:
                self._balances[key] = 100
            balance = self._balances[key]

            # Apply daily decay if needed
            if balance > DECAY_THRESHOLD and decay_tracker.should_apply_decay(guild_id, user_id):
                decay_amount = int(balance * DECAY_PERCENT)
                self._balances[key] = balance - decay_amount
                self.save()
                decay_tracker.mark_processed(guild_id, user_id)
                balance = self._balances[key]

            return balance

    async def add_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        """Add coins to user balance. Returns new balance."""
        if amount < 0:
            raise ValueError("Amount must be positive")

        key = f"{guild_id}:{user_id}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Одна транзакция: decay → cap → начисление.
                    # SELECT ... FOR UPDATE сериализует конкурентные
                    # операции над одним балансом.
                    await self._ensure_row(conn, guild_id, user_id)
                    row = await conn.fetchrow(
                        "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2 FOR UPDATE",
                        guild_id, user_id
                    )
                    balance = await self._apply_decay(
                        conn, guild_id, user_id, row["balance"]
                    )
                    add = min(amount, max(0, MAX_BALANCE - balance))
                    new_balance = balance + add
                    await conn.execute(
                        "UPDATE user_balance SET balance = $1 WHERE guild_id = $2 AND user_id = $3",
                        new_balance, guild_id, user_id
                    )
                    return new_balance
        else:
            if key not in self._balances:
                self._balances[key] = 100
            self._balances[key] = min(self._balances[key] + amount, MAX_BALANCE)
            self.save()
            return self._balances[key]

    async def subtract_balance(self, guild_id: int, user_id: int, amount: int) -> int:
        """Subtract coins from user balance. Returns new balance.

        Проверка достаточности и списание выполняются атомарно
        в одной транзакции под блокировкой строки (FOR UPDATE) —
        двойное списание и отрицательный баланс исключены.
        """
        if amount < 0:
            raise ValueError("Amount must be positive")

        key = f"{guild_id}:{user_id}"

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self._ensure_row(conn, guild_id, user_id)
                    row = await conn.fetchrow(
                        "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2 FOR UPDATE",
                        guild_id, user_id
                    )
                    balance = await self._apply_decay(
                        conn, guild_id, user_id, row["balance"]
                    )
                    if balance < amount:
                        raise ValueError("Insufficient balance")
                    new_balance = balance - amount
                    await conn.execute(
                        "UPDATE user_balance SET balance = $1 WHERE guild_id = $2 AND user_id = $3",
                        new_balance, guild_id, user_id
                    )
                    return new_balance
        else:
            if key not in self._balances:
                self._balances[key] = 100
            balance = self._balances[key]
            # Decay — как в get_balance (сохраняем старую семантику)
            if balance > DECAY_THRESHOLD and decay_tracker.should_apply_decay(guild_id, user_id):
                decay_amount = int(balance * DECAY_PERCENT)
                balance -= decay_amount
                self._balances[key] = balance
                self.save()
                decay_tracker.mark_processed(guild_id, user_id)
            if balance < amount:
                raise ValueError("Insufficient balance")
            self._balances[key] = balance - amount
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

    async def transfer_balance(self, from_guild: int, from_user: int, to_guild: int, to_user: int, amount: int, fee_percent: float = 0.1) -> dict:
        """Transfer coins from one user to another with fee.
        
        Args:
            from_guild: Sender's guild ID
            from_user: Sender's user ID
            to_guild: Receiver's guild ID
            to_user: Receiver's user ID
            amount: Amount to transfer (before fee)
            fee_percent: Fee percentage (default 0.1 = 10%)
            
        Returns:
            Dict with transfer details: {
                'amount': amount,
                'fee': fee_amount,
                'total_deducted': total,
                'from_balance': sender_new_balance,
                'to_balance': receiver_new_balance
            }
        """
        if amount < 0:
            raise ValueError("Amount must be positive")
        
        if from_user == to_user:
            raise ValueError("Cannot transfer to yourself")
        
        fee = int(amount * fee_percent)
        total = amount + fee

        if self._use_db:
            # Атомарный перевод: одна транзакция, обе строки под
            # FOR UPDATE — списание и начисление либо обе, либо ни одной.
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self._ensure_row(conn, from_guild, from_user)
                    row = await conn.fetchrow(
                        "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2 FOR UPDATE",
                        from_guild, from_user
                    )
                    sender_balance = await self._apply_decay(
                        conn, from_guild, from_user, row["balance"]
                    )
                    if sender_balance < total:
                        raise ValueError(
                            f"Insufficient balance. Need {total} but have {sender_balance}"
                        )
                    await conn.execute(
                        "UPDATE user_balance SET balance = $1 WHERE guild_id = $2 AND user_id = $3",
                        sender_balance - total, from_guild, from_user
                    )

                    await self._ensure_row(conn, to_guild, to_user)
                    row = await conn.fetchrow(
                        "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2 FOR UPDATE",
                        to_guild, to_user
                    )
                    receiver_balance = await self._apply_decay(
                        conn, to_guild, to_user, row["balance"]
                    )
                    receiver_new = min(receiver_balance + amount, MAX_BALANCE)
                    await conn.execute(
                        "UPDATE user_balance SET balance = $1 WHERE guild_id = $2 AND user_id = $3",
                        receiver_new, to_guild, to_user
                    )

                    return {
                        'amount': amount,
                        'fee': fee,
                        'total_deducted': total,
                        'from_balance': sender_balance - total,
                        'to_balance': receiver_new,
                    }

        # JSON fallback: однопроцессный asyncio — проверки и записи
        # в async-методах без внутренних await атомарны для event loop.
        sender_balance = await self.get_balance(from_guild, from_user)
        if sender_balance < total:
            raise ValueError(f"Insufficient balance. Need {total} but have {sender_balance}")

        await self.subtract_balance(from_guild, from_user, total)
        await self.add_balance(to_guild, to_user, amount)

        sender_new_balance = await self.get_balance(from_guild, from_user)
        receiver_new_balance = await self.get_balance(to_guild, to_user)

        return {
            'amount': amount,
            'fee': fee,
            'total_deducted': total,
            'from_balance': sender_new_balance,
            'to_balance': receiver_new_balance
        }

    async def reset_user(self, guild_id: int, user_id: int) -> None:
        """Reset user balance to default (100 coins)."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM user_balance WHERE guild_id = $1 AND user_id = $2",
                    guild_id, user_id
                )
        else:
            key = f"{guild_id}:{user_id}"
            if key in self._balances:
                del self._balances[key]
                self.save()


user_balance_store = UserBalanceStore()
