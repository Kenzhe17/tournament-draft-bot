"""Storage for betting system with dynamic odds and buffer system."""

import asyncio
import json
from pathlib import Path
from typing import Any

from models.bet import Bet, MatchOdds

DATA_DIR = Path("data")
BETS_FILE = DATA_DIR / "bets.json"
ODDS_FILE = DATA_DIR / "odds.json"


class BetStore:
    """Хранилище ставок с динамическими коэффициентами и буфером."""

    def __init__(self) -> None:
        self._bets: dict[str, list[Bet]] = {}  # match_id -> list of bets
        self._odds: dict[str, MatchOdds] = {}  # match_id -> current odds and buffer
        self._locks: dict[str, asyncio.Lock] = {}  # match_id -> async lock for race condition protection
        self._use_db = True  # Использовать PostgreSQL
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def _get_lock(self, match_id: str) -> asyncio.Lock:
        """Get or create a lock for the match."""
        if match_id not in self._locks:
            self._locks[match_id] = asyncio.Lock()
        return self._locks[match_id]

    def load(self) -> None:
        """Загрузить ставки и коэффициенты из файла (fallback)."""
        if not BETS_FILE.exists():
            return

        try:
            with open(BETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for match_id, bets_data in data.items():
                    self._bets[match_id] = [Bet.from_dict(b) for b in bets_data]
        except (json.JSONDecodeError, KeyError):
            self._bets = {}

        # Load odds
        if ODDS_FILE.exists():
            try:
                with open(ODDS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for match_id, odds_data in data.items():
                        self._odds[match_id] = MatchOdds.from_dict(odds_data)
            except (json.JSONDecodeError, KeyError):
                self._odds = {}

    def save(self) -> None:
        """Сохранить ставки и коэффициенты в файл (fallback)."""
        data = {
            match_id: [bet.to_dict() for bet in bets]
            for match_id, bets in self._bets.items()
        }
        with open(BETS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # Save odds
        odds_data = {
            match_id: odds.to_dict()
            for match_id, odds in self._odds.items()
        }
        with open(ODDS_FILE, "w", encoding="utf-8") as f:
            json.dump(odds_data, f, ensure_ascii=False, indent=2)

    def initialize_match_odds(self, match_id: str, team_a_name: str, team_b_name: str, avg_elo_a: float, avg_elo_b: float) -> None:
        """Initialize odds for a match based on ELO difference."""
        # Calculate initial odds based on ELO
        elo_diff = avg_elo_b - avg_elo_a
        odds_diff = elo_diff * 0.002
        base_odds = 1.9

        if elo_diff >= 0:
            # Team B has higher ELO
            odds_a = base_odds + odds_diff
            odds_b = base_odds - odds_diff
        else:
            # Team A has higher ELO
            odds_a = base_odds - abs(odds_diff)
            odds_b = base_odds + abs(odds_diff)

        # Ensure minimum odds of 1.1x
        odds_a = max(1.1, odds_a)
        odds_b = max(1.1, odds_b)

        self._odds[match_id] = MatchOdds(
            team_a_odds=odds_a,
            team_b_odds=odds_b,
            team_a_buffer=0,
            team_b_buffer=0
        )

    async def initialize_match_odds_db(self, match_id: str, team_a_odds: float, team_b_odds: float) -> None:
        """Initialize odds in database."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO match_odds (match_id, team_a_odds, team_b_odds, team_a_buffer, team_b_buffer)
                    VALUES ($1, $2, $3, 0, 0)
                    ON CONFLICT (match_id) DO UPDATE SET
                        team_a_odds = $2, team_b_odds = $3, team_a_buffer = 0, team_b_buffer = 0
                    """,
                    match_id, team_a_odds, team_b_odds
                )

    def get_current_odds(self, match_id: str) -> MatchOdds | None:
        """Get current odds for a match."""
        if self._use_db:
            # For now, use in-memory cache (will be loaded on init in production)
            return self._odds.get(match_id)
        return self._odds.get(match_id)

    async def get_current_odds_db(self, match_id: str) -> MatchOdds | None:
        """Get current odds from database."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT team_a_odds, team_b_odds, team_a_buffer, team_b_buffer FROM match_odds WHERE match_id = $1",
                    match_id
                )
                if row:
                    return MatchOdds(
                        team_a_odds=row["team_a_odds"],
                        team_b_odds=row["team_b_odds"],
                        team_a_buffer=row["team_a_buffer"],
                        team_b_buffer=row["team_b_buffer"]
                    )
        return None

    async def save_bet(self, bet: Bet, team_a_name: str, team_b_name: str) -> None:
        """Сохранить ставку с динамическим расчётом коэффициентов."""
        # Get lock for this match to prevent race conditions
        lock = self._get_lock(bet.match_id)
        async with lock:
            # Ensure odds are initialized
            if bet.match_id not in self._odds:
                # Initialize with default odds if not set
                self._odds[bet.match_id] = MatchOdds(
                    team_a_odds=1.9,
                    team_b_odds=1.9,
                    team_a_buffer=0,
                    team_b_buffer=0
                )

            # Get current odds
            current_odds = self._odds[bet.match_id]

            # Determine which team was bet on
            if bet.team_name == team_a_name:
                is_team_a = True
                current_bet_odds = current_odds.team_a_odds
            elif bet.team_name == team_b_name:
                is_team_a = False
                current_bet_odds = current_odds.team_b_odds
            else:
                raise ValueError(f"Invalid team name: {bet.team_name}")

            # Fix the odds at the time of betting
            bet.odds = current_bet_odds

            # Calculate odds shift: 100 coins = 0.1x shift
            odds_shift = bet.amount / 100 * 0.1

            # Apply dynamic odds with buffer logic
            if is_team_a:
                # Betting on team A: team A odds decrease, team B odds increase
                # First, check if team B has buffer to absorb
                if current_odds.team_b_buffer > 0:
                    # Use buffer first
                    buffer_reduction = min(current_odds.team_b_buffer, bet.amount)
                    current_odds.team_b_buffer -= buffer_reduction
                    remaining_bet = bet.amount - buffer_reduction

                    if remaining_bet > 0:
                        # Calculate shift for remaining amount
                        remaining_shift = remaining_bet / 100 * 0.1

                        # Apply shift with floor check
                        new_odds_a = current_odds.team_a_odds - remaining_shift
                        if new_odds_a < 1.1:
                            # Hit floor, remaining goes to buffer
                            overflow = (1.1 - new_odds_a) / 0.1 * 100
                            current_odds.team_a_odds = 1.1
                            current_odds.team_a_buffer += int(overflow)
                        else:
                            current_odds.team_a_odds = new_odds_a

                        # Increase team B odds
                        current_odds.team_b_odds = min(3.0, current_odds.team_b_odds + remaining_shift)
                else:
                    # No buffer, apply shift directly
                    new_odds_a = current_odds.team_a_odds - odds_shift
                    if new_odds_a < 1.1:
                        # Hit floor, remaining goes to buffer
                        overflow = (1.1 - new_odds_a) / 0.1 * 100
                        current_odds.team_a_odds = 1.1
                        current_odds.team_a_buffer += int(overflow)
                    else:
                        current_odds.team_a_odds = new_odds_a

                    # Increase team B odds
                    current_odds.team_b_odds = min(3.0, current_odds.team_b_odds + odds_shift)
            else:
                # Betting on team B: team B odds decrease, team A odds increase
                # First, check if team A has buffer to absorb
                if current_odds.team_a_buffer > 0:
                    # Use buffer first
                    buffer_reduction = min(current_odds.team_a_buffer, bet.amount)
                    current_odds.team_a_buffer -= buffer_reduction
                    remaining_bet = bet.amount - buffer_reduction

                    if remaining_bet > 0:
                        # Calculate shift for remaining amount
                        remaining_shift = remaining_bet / 100 * 0.1

                        # Apply shift with floor check
                        new_odds_b = current_odds.team_b_odds - remaining_shift
                        if new_odds_b < 1.1:
                            # Hit floor, remaining goes to buffer
                            overflow = (1.1 - new_odds_b) / 0.1 * 100
                            current_odds.team_b_odds = 1.1
                            current_odds.team_b_buffer += int(overflow)
                        else:
                            current_odds.team_b_odds = new_odds_b

                        # Increase team A odds
                        current_odds.team_a_odds = min(3.0, current_odds.team_a_odds + remaining_shift)
                else:
                    # No buffer, apply shift directly
                    new_odds_b = current_odds.team_b_odds - odds_shift
                    if new_odds_b < 1.1:
                        # Hit floor, remaining goes to buffer
                        overflow = (1.1 - new_odds_b) / 0.1 * 100
                        current_odds.team_b_odds = 1.1
                        current_odds.team_b_buffer += int(overflow)
                    else:
                        current_odds.team_b_odds = new_odds_b

                    # Increase team A odds
                    current_odds.team_a_odds = min(3.0, current_odds.team_a_odds + odds_shift)

            # Save the bet (DB or file)
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
                        INSERT INTO bets (guild_id, user_id, user_name, match_id, team_name, amount, odds)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (guild_id, user_id, match_id)
                        DO UPDATE SET user_name = $3, team_name = $5, amount = $6, odds = $7
                        """,
                        bet.guild_id, bet.user_id, bet.user_name, bet.match_id, bet.team_name, bet.amount, bet.odds
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
                self.save()  # Save odds as well

    async def get_bets_by_match(self, match_id: str) -> list[Bet]:
        """Получить все ставки для матча."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT guild_id, user_id, user_name, match_id, team_name, amount, odds FROM bets WHERE match_id = $1",
                    match_id
                )
                return [Bet(guild_id=row["guild_id"], user_id=row["user_id"], user_name=row["user_name"], match_id=row["match_id"], team_name=row["team_name"], amount=row["amount"], odds=row["odds"]) for row in rows]
        else:
            return self._bets.get(match_id, [])

    async def get_user_bet(self, guild_id: int, user_id: int, match_id: str) -> Bet | None:
        """Получить ставку пользователя на матч."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT guild_id, user_id, user_name, match_id, team_name, amount, odds FROM bets WHERE guild_id = $1 AND user_id = $2 AND match_id = $3",
                    guild_id, user_id, match_id
                )
                if row:
                    return Bet(guild_id=row["guild_id"], user_id=row["user_id"], user_name=row["user_name"], match_id=row["match_id"], team_name=row["team_name"], amount=row["amount"], odds=row["odds"])
                return None
        else:
            bets = self._bets.get(match_id, [])
            for bet in bets:
                if bet.user_id == user_id and bet.guild_id == guild_id:
                    return bet
            return None

    async def delete_bets_by_match(self, match_id: str) -> None:
        """Удалить все ставки для матча."""
        # Also clear odds for this match
        if match_id in self._odds:
            del self._odds[match_id]
        if match_id in self._locks:
            del self._locks[match_id]

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

        # Calculate payouts based on fixed odds at time of betting
        for bet in bets:
            if bet.team_name == winning_team_name:
                # Winning bet: payout = amount * odds (fixed at betting time)
                payout = int(bet.amount * bet.odds)
                payouts[bet.user_id] = payout
                # Record as win (profit = payout - bet_amount)
                profit = payout - bet.amount
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, profit, won=True)
            else:
                # Losing bet: no payout
                payouts[bet.user_id] = 0
                # Record as loss (bet amount lost)
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, bet.amount, won=False)

        # Delete bets after resolution
        await self.delete_bets_by_match(match_id)

        return payouts

    def enable_db(self) -> None:
        """Enable database mode."""
        self._use_db = True


# Глобальный экземпляр хранилища
bet_store = BetStore()
