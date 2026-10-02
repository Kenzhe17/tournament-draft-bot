"""Хранилище баланса банковского сейфа пользователей."""

import os
import json
from pathlib import Path
from typing import Optional

import asyncpg

from database import get_db_connection


class UserBankStore:
    """Хранилище баланса банковского сейфа."""

    def __init__(self):
        self._db_mode = bool(os.getenv("DATABASE_URL"))
        self._data_file = Path("data/user_bank.json")
        self._data = {}

        if not self._db_mode:
            self._load_from_file()

    def _load_from_file(self) -> None:
        """Загрузить данные из JSON файла."""
        if self._data_file.exists():
            try:
                with open(self._data_file, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._data = {}

    def _save_to_file(self) -> None:
        """Сохранить данные в JSON файл."""
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._data_file, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def _get_key(self, guild_id: int, user_id: int) -> str:
        """Получить ключ для хранения."""
        return f"{guild_id}:{user_id}"

    async def get_bank_balance(self, guild_id: int, user_id: int) -> int:
        """Получить баланс в сейфе."""
        if self._db_mode:
            conn = await get_db_connection()
            row = await conn.fetchrow(
                "SELECT bank_balance FROM user_bank WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id
            )
            await conn.close()
            return row["bank_balance"] if row else 0
        else:
            key = self._get_key(guild_id, user_id)
            return self._data.get(key, 0)

    async def set_bank_balance(self, guild_id: int, user_id: int, balance: int) -> None:
        """Установить баланс в сейфе."""
        if self._db_mode:
            conn = await get_db_connection()
            await conn.execute(
                """
                INSERT INTO user_bank (guild_id, user_id, bank_balance)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET bank_balance = $3
                """,
                guild_id, user_id, balance
            )
            await conn.close()
        else:
            key = self._get_key(guild_id, user_id)
            self._data[key] = balance
            self._save_to_file()

    async def add_to_bank(self, guild_id: int, user_id: int, amount: int) -> None:
        """Добавить монеты в сейф."""
        current = await self.get_bank_balance(guild_id, user_id)
        await self.set_bank_balance(guild_id, user_id, current + amount)

    async def subtract_from_bank(self, guild_id: int, user_id: int, amount: int) -> None:
        """Вычесть монеты из сейфа."""
        current = await self.get_bank_balance(guild_id, user_id)
        await self.set_bank_balance(guild_id, user_id, max(0, current - amount))


# Глобальный экземпляр хранилища
user_bank_store = UserBankStore()
