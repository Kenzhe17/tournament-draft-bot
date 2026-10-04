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
        self._bets: dict[str, list[Bet]] = {}  # (tournament_id:match_id) -> list of bets
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
                        # Handle old format without team names
                        if "team_a_name" not in odds_data:
                            odds_data["team_a_name"] = ""
                            odds_data["team_b_name"] = ""
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
        # Higher ELO team gets lower odds
        # 100 ELO difference = 0.1x odds difference
        elo_diff = avg_elo_b - avg_elo_a
        odds_diff = elo_diff * 0.001
        base_odds = 1.9

        if elo_diff >= 0:
            # Team B has higher ELO -> Team B gets lower odds
            odds_a = base_odds + odds_diff  # Team A gets higher odds
            odds_b = base_odds - odds_diff  # Team B gets lower odds
        else:
            # Team A has higher ELO -> Team A gets lower odds
            odds_a = base_odds + odds_diff  # Team A gets lower odds (odds_diff is negative)
            odds_b = base_odds - odds_diff  # Team B gets higher odds

        # Ensure minimum odds of 1.1x and maximum of 2.7x
        odds_a = max(1.1, min(2.7, odds_a))
        odds_b = max(1.1, min(2.7, odds_b))

        self._odds[match_id] = MatchOdds(
            team_a_name=team_a_name,
            team_b_name=team_b_name,
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
        odds = self._odds.get(match_id)
        if odds:
            # Apply limits to ensure odds are within bounds
            odds.team_a_odds = max(1.1, min(2.7, odds.team_a_odds))
            odds.team_b_odds = max(1.1, min(2.7, odds.team_b_odds))
        return odds

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
                    # Apply limits to ensure odds are within bounds
                    team_a_odds = max(1.1, min(2.7, row["team_a_odds"]))
                    team_b_odds = max(1.1, min(2.7, row["team_b_odds"]))
                    return MatchOdds(
                        team_a_odds=team_a_odds,
                        team_b_odds=team_b_odds,
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
                    team_a_name=team_a_name,
                    team_b_name=team_b_name,
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

            # Check if user already has a bet
            existing_bet = await self.get_user_bet(bet.guild_id, bet.user_id, bet.tournament_id, bet.match_id)
            if existing_bet:
                # Update existing bet, keep original odds
                additional_amount = bet.amount - existing_bet.amount
                bet.amount += existing_bet.amount  # Update to total amount
                bet.odds = existing_bet.odds
                # Always shift odds by absolute amount (even when decreasing)
                # Any bet on a team decreases its odds, regardless of amount change
                shift_amount = abs(additional_amount)
            else:
                # New bet, use current odds
                bet.odds = current_bet_odds
                shift_amount = bet.amount

            # Calculate odds shift: 500 coins = 0.1x shift (more responsive)
            shift_odds = shift_amount / 500 * 0.1

            # Apply dynamic odds with buffer logic
            if is_team_a:
                # Betting on team A: team A odds decrease (becomes more attractive), team B odds increase
                # First, check if team B has buffer to absorb
                if current_odds.team_b_buffer > 0:
                    # Use buffer first
                    buffer_reduction = min(current_odds.team_b_buffer, shift_amount)
                    current_odds.team_b_buffer -= buffer_reduction
                    remaining_bet = shift_amount - buffer_reduction

                    if remaining_bet > 0:
                        # Calculate shift for remaining amount
                        remaining_shift = remaining_bet / 500 * 0.1

                        # Apply shift with floor check
                        new_odds_a = current_odds.team_a_odds - remaining_shift
                        if new_odds_a < 1.1:
                            # Hit floor, remaining goes to buffer
                            overflow = (1.1 - new_odds_a) / 0.1 * 500
                            current_odds.team_a_odds = 1.1
                            current_odds.team_a_buffer += int(overflow)
                        else:
                            current_odds.team_a_odds = new_odds_a

                        # Increase team B odds
                        current_odds.team_b_odds = min(2.7, current_odds.team_b_odds + remaining_shift)
                else:
                    # No buffer, apply shift directly
                    new_odds_a = current_odds.team_a_odds - shift_odds
                    if new_odds_a < 1.1:
                        # Hit floor, remaining goes to buffer
                        overflow = (1.1 - new_odds_a) / 0.1 * 500
                        current_odds.team_a_odds = 1.1
                        current_odds.team_a_buffer += int(overflow)
                    else:
                        current_odds.team_a_odds = new_odds_a

                    # Increase team B odds
                    current_odds.team_b_odds = min(2.7, current_odds.team_b_odds + shift_odds)
            else:
                # Betting on team B: team B odds decrease (becomes more attractive), team A odds increase
                # First, check if team A has buffer to absorb
                if current_odds.team_a_buffer > 0:
                    # Use buffer first
                    buffer_reduction = min(current_odds.team_a_buffer, shift_amount)
                    current_odds.team_a_buffer -= buffer_reduction
                    remaining_bet = shift_amount - buffer_reduction

                    if remaining_bet > 0:
                        # Calculate shift for remaining amount
                        remaining_shift = remaining_bet / 500 * 0.1

                        # Apply shift with floor check
                        new_odds_b = current_odds.team_b_odds - remaining_shift
                        if new_odds_b < 1.1:
                            # Hit floor, remaining goes to buffer
                            overflow = (1.1 - new_odds_b) / 0.1 * 500
                            current_odds.team_b_odds = 1.1
                            current_odds.team_b_buffer += int(overflow)
                        else:
                            current_odds.team_b_odds = new_odds_b

                        # Increase team A odds
                        current_odds.team_a_odds = min(2.7, current_odds.team_a_odds + remaining_shift)
                else:
                    # No buffer, apply shift directly
                    new_odds_b = current_odds.team_b_odds - shift_odds
                    if new_odds_b < 1.1:
                        # Hit floor, remaining goes to buffer
                        overflow = (1.1 - new_odds_b) / 0.1 * 500
                        current_odds.team_b_odds = 1.1
                        current_odds.team_b_buffer += int(overflow)
                    else:
                        current_odds.team_b_odds = new_odds_b

                    # Increase team A odds
                    current_odds.team_a_odds = min(2.7, current_odds.team_a_odds + shift_odds)

            # Save the bet (DB or file)
            if self._use_db:
                from storage.db import get_pool
                from storage.user_balance_store import user_balance_store
                pool = await get_pool()
                async with pool.acquire() as conn:
                    # Check if user already has a bet on this match
                    existing_bet = await self.get_user_bet(bet.guild_id, bet.user_id, bet.tournament_id, bet.match_id)
                    if existing_bet:
                        # Bet already exists - do nothing (only one bet per match)
                        return

                    await conn.execute(
                        """
                        INSERT INTO bets (guild_id, user_id, user_name, tournament_id, match_id, team_name, team_index, amount, odds)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (guild_id, user_id, tournament_id, match_id)
                        DO NOTHING
                        """,
                        bet.guild_id, bet.user_id, bet.user_name, bet.tournament_id, bet.match_id, bet.team_name, bet.team_index, bet.amount, bet.odds
                    )
            else:
                from storage.user_balance_store import user_balance_store

                key = f"{bet.tournament_id}:{bet.match_id}"
                if key not in self._bets:
                    self._bets[key] = []

                # Check if user already has a bet on this match
                existing_idx = next(
                    (i for i, b in enumerate(self._bets[key]) if b.user_id == bet.user_id),
                    None
                )
                if existing_idx is not None:
                    # Bet already exists - do nothing (only one bet per match)
                    return

                self._bets[key].append(bet)
                self.save()
                self.save()  # Save odds as well

    async def get_bets_by_match(self, tournament_id: str, match_id: str) -> list[Bet]:
        """Получить все ставки для матча."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT guild_id, user_id, user_name, tournament_id, match_id, team_name, team_index, amount, odds FROM bets WHERE tournament_id = $1 AND match_id = $2",
                    tournament_id, match_id
                )
                return [Bet(guild_id=row["guild_id"], user_id=row["user_id"], user_name=row["user_name"], tournament_id=row["tournament_id"], match_id=row["match_id"], team_name=row["team_name"], team_index=row.get("team_index", 0), amount=row["amount"], odds=row["odds"]) for row in rows]
        else:
            key = f"{tournament_id}:{match_id}"
            return self._bets.get(key, [])

    async def get_user_bet(self, guild_id: int, user_id: int, tournament_id: str, match_id: str) -> Bet | None:
        """Получить ставку пользователя на матч."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT guild_id, user_id, user_name, tournament_id, match_id, team_name, team_index, amount, odds FROM bets WHERE guild_id = $1 AND user_id = $2 AND tournament_id = $3 AND match_id = $4",
                    guild_id, user_id, tournament_id, match_id
                )
                if row:
                    return Bet(guild_id=row["guild_id"], user_id=row["user_id"], user_name=row["user_name"], tournament_id=row["tournament_id"], match_id=row["match_id"], team_name=row["team_name"], team_index=row.get("team_index", 0), amount=row["amount"], odds=row["odds"])
                return None
        else:
            bets = self._bets.get(match_id, [])
            for bet in bets:
                if bet.user_id == user_id and bet.guild_id == guild_id and bet.tournament_id == tournament_id:
                    return bet
            return None

    async def delete_bets_by_match(self, tournament_id: str, match_id: str) -> None:
        """Удалить все ставки для матча."""
        # DO NOT delete odds - keep them for display
        # Odds should only be cleared when tournament is deleted, not when match is resolved

        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM bets WHERE tournament_id = $1 AND match_id = $2", tournament_id, match_id)
        else:
            key = f"{tournament_id}:{match_id}"
            if key in self._bets:
                del self._bets[key]
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
                key: [b for b in bets if b.guild_id == guild_id]
                for key, bets in self._bets.items()
            }
            self.save()

    async def resolve_match_bets(self, guild_id: int, tournament_id: str, match_id: str, winning_team_name: str, winning_team_index: int) -> tuple[dict[int, int], dict[str, int]]:
        """Resolve bets for a match and return payouts (user_id -> amount) and payouts by name (user_name -> amount)."""
        from storage.betting_stats_store import betting_stats_store
        import logging

        bets = await self.get_bets_by_match(tournament_id, match_id)
        payouts = {}
        payouts_by_name = {}

        logging.info(f"Resolving bets for tournament_id={tournament_id}, match_id={match_id}, winning_team_name='{winning_team_name}', winning_team_index={winning_team_index}")
        logging.info(f"Found {len(bets)} bets for this match")

        # Get current odds for the winning team (final odds)
        current_odds = self.get_current_odds(match_id)
        if current_odds:
            logging.info(f"Current odds: team_a='{current_odds.team_a_name}' ({current_odds.team_a_odds}x), team_b='{current_odds.team_b_name}' ({current_odds.team_b_odds}x)")
            # Determine which team won based on team index
            if winning_team_index == 0:
                winning_odds = current_odds.team_a_odds
                logging.info(f"Matched to team_a (index 0) with odds {winning_odds}x")
            elif winning_team_index == 1:
                winning_odds = current_odds.team_b_odds
                logging.info(f"Matched to team_b (index 1) with odds {winning_odds}x")
            else:
                # Fallback: use default
                logging.warning(f"Invalid team_index: {winning_team_index}, using default 1.9x")
                winning_odds = 1.9
        else:
            logging.warning(f"No odds found for match_id={match_id}, using default 1.9x")
            winning_odds = 1.9  # Default if no odds stored

        # Calculate payouts based on final odds (not fixed at betting time)
        for bet in bets:
            logging.info(f"Checking bet: user_id={bet.user_id}, team_name='{bet.team_name}', team_index={bet.team_index}, amount={bet.amount}, odds={bet.odds}")
            if bet.team_index == winning_team_index:
                # Winning bet: payout = amount * final odds
                payout = int(bet.amount * winning_odds)
                payouts[bet.user_id] = payout
                payouts_by_name[bet.user_name] = payout
                logging.info(f"WINNING bet: user_id={bet.user_id}, team_index={bet.team_index} == {winning_team_index}, payout={payout}")
                # Record as win (profit = payout - bet_amount)
                profit = payout - bet.amount
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, profit, won=True)
            else:
                # Losing bet: no payout
                payouts[bet.user_id] = 0
                payouts_by_name[bet.user_name] = 0
                logging.info(f"LOSING bet: user_id={bet.user_id}, team_index={bet.team_index} != {winning_team_index}")
                # Record as loss (bet amount lost)
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, bet.amount, won=False)

        # Delete bets after resolution
        await self.delete_bets_by_match(tournament_id, match_id)

        logging.info(f"Final payouts: {payouts}")
        return payouts, payouts_by_name

    def enable_db(self) -> None:
        """Enable database mode."""
        self._use_db = True


# Глобальный экземпляр хранилища
bet_store = BetStore()
