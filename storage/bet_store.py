"""Storage for betting system."""

import json
from pathlib import Path
from typing import Any

from models.bet import Bet

DATA_DIR = Path("data")
BETS_FILE = DATA_DIR / "bets.json"


class BetStore:
    """Хранилище ставок в PostgreSQL."""

    def __init__(self) -> None:
        self._bets: dict[str, list[Bet]] = {}  # match_id -> list of bets
        self._use_db = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить ставки из файла (fallback)."""
        if not BETS_FILE.exists():
            return

        try:
            with open(BETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for match_id, bets_data in data.items():
                    self._bets[match_id] = [Bet.from_dict(b) for b in bets_data]
        except (json.JSONDecodeError, KeyError):
            self._bets = {}

    def save(self) -> None:
        """Сохранить ставки в файл (fallback)."""
        data = {
            match_id: [bet.to_dict() for bet in bets]
            for match_id, bets in self._bets.items()
        }
        with open(BETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def save_bet(self, bet: Bet) -> None:
        """Сохранить ставку."""
        if self._use_db:
            from storage.db import get_pool
            from storage.user_balance_store import user_balance_store
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Check if user already has a bet on this match
                existing_bet = await self.get_user_bet(bet.guild_id, bet.user_id, bet.match_id)
                if existing_bet:
                    # Refund previous bet amount
                    await user_balance_store.add_balance(bet.guild_id, bet.user_id, existing_bet.amount)

                await conn.execute(
                    """
                    INSERT INTO bets (guild_id, user_id, user_name, match_id, team_name, amount)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (guild_id, user_id, match_id)
                    DO UPDATE SET user_name = $3, team_name = $5, amount = $6
                    """,
                    bet.guild_id, bet.user_id, bet.user_name, bet.match_id, bet.team_name, bet.amount
                )
        else:
            from storage.user_balance_store import user_balance_store

            if bet.match_id not in self._bets:
                self._bets[bet.match_id] = []

            # Check if user already has a bet on this match
            existing_idx = next(
                (i for i, b in enumerate(self._bets[bet.match_id]) if b.user_id == bet.user_id),
                None
            )
            if existing_idx is not None:
                # Refund previous bet amount
                existing_bet = self._bets[bet.match_id][existing_idx]
                await user_balance_store.add_balance(bet.guild_id, bet.user_id, existing_bet.amount)
                # Replace with new bet
                self._bets[bet.match_id][existing_idx] = bet
            else:
                self._bets[bet.match_id].append(bet)

            self.save()

    async def get_bets_by_match(self, match_id: str) -> list[Bet]:
        """Получить все ставки для матча."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT guild_id, user_id, user_name, match_id, team_name, amount FROM bets WHERE match_id = $1",
                    match_id
                )
                return [Bet(guild_id=row["guild_id"], user_id=row["user_id"], user_name=row["user_name"], match_id=row["match_id"], team_name=row["team_name"], amount=row["amount"]) for row in rows]
        else:
            return self._bets.get(match_id, [])

    async def get_user_bet(self, guild_id: int, user_id: int, match_id: str) -> Bet | None:
        """Получить ставку пользователя на матч."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT guild_id, user_id, user_name, match_id, team_name, amount FROM bets WHERE guild_id = $1 AND user_id = $2 AND match_id = $3",
                    guild_id, user_id, match_id
                )
                if row:
                    return Bet(guild_id=row["guild_id"], user_id=row["user_id"], user_name=row["user_name"], match_id=row["match_id"], team_name=row["team_name"], amount=row["amount"])
                return None
        else:
            bets = self._bets.get(match_id, [])
            for bet in bets:
                if bet.user_id == user_id and bet.guild_id == guild_id:
                    return bet
            return None

    async def delete_bets_by_match(self, match_id: str) -> None:
        """Удалить все ставки для матча."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM bets WHERE match_id = $1", match_id)
        else:
            if match_id in self._bets:
                del self._bets[match_id]
                self.save()

    async def delete_bets_by_guild(self, guild_id: int) -> None:
        """Удалить все ставки для сервера."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM bets WHERE guild_id = $1", guild_id)
        else:
            # Filter out bets from this guild
            self._bets = {
                match_id: [b for b in bets if b.guild_id == guild_id]
                for match_id, bets in self._bets.items()
            }
            self.save()

    async def resolve_match_bets(self, guild_id: int, match_id: str, winning_team_name: str) -> dict[int, int]:
        """Resolve bets for a match and return payouts (user_id -> amount)."""
        from storage.betting_stats_store import betting_stats_store

        bets = await self.get_bets_by_match(match_id)
        payouts = {}

        # Calculate total bank and winning team bets
        total_bank = sum(b.amount for b in bets)
        winning_bets = [b for b in bets if b.team_name == winning_team_name]
        winning_total = sum(b.amount for b in winning_bets)

        if winning_total == 0:
            # No one bet on the winner, return all bets to their owners
            for bet in bets:
                payouts[bet.user_id] = bet.amount
                # Record as loss (bet amount lost)
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, bet.amount, won=False)
        else:
            # Calculate payout ratio (total bank / winning bets total)
            payout_ratio = total_bank / winning_total

            # Distribute winnings
            for bet in winning_bets:
                payout = int(bet.amount * payout_ratio)
                payouts[bet.user_id] = payout
                # Record as win (profit = payout - bet_amount)
                profit = payout - bet.amount
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, profit, won=True)

            # Record losses for losing bets
            losing_bets = [b for b in bets if b.team_name != winning_team_name]
            for bet in losing_bets:
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, bet.amount, won=False)

        # Delete bets after resolution
        await self.delete_bets_by_match(match_id)

        return payouts

    def enable_db(self) -> None:
        """Enable database mode."""
        self._use_db = True


# Глобальный экземпляр хранилища
bet_store = BetStore()
