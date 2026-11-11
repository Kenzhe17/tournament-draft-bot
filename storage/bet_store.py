"""Storage for betting system with dynamic odds and buffer system."""

import asyncio
import json
from pathlib import Path
from typing import Any

from models.bet import Bet, MatchOdds

from config import DATA_DIR  # noqa: E402 — единый путь из config
from storage.json_atomic import atomic_write_json
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
        # Load JSON fallback only, PostgreSQL loading is deferred to async context
        if not self._use_db:
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

    async def load_from_db(self) -> None:
        """Load odds from PostgreSQL after bot startup."""
        if not self._use_db:
            return
        try:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM match_odds")
                loaded = {}
                for row in rows:
                    loaded[row["match_id"]] = MatchOdds(
                        team_a_name="",
                        team_b_name="",
                        team_a_odds=row["team_a_odds"],
                        team_b_odds=row["team_b_odds"],
                        team_a_buffer=row["team_a_buffer"],
                        team_b_buffer=row["team_b_buffer"]
                    )
                self._odds = loaded
        except Exception as exc:
            print(f"Error loading odds from DB: {exc}")
            self._odds = {}

    def save(self) -> None:
        """Сохранить ставки и коэффициенты в файл (fallback)."""
        data = {
            match_id: [bet.to_dict() for bet in bets]
            for match_id, bets in self._bets.items()
        }
        atomic_write_json(BETS_FILE, data)

        # Save odds
        odds_data = {
            match_id: odds.to_dict()
            for match_id, odds in self._odds.items()
        }
        atomic_write_json(ODDS_FILE, odds_data)

    def initialize_match_odds(self, match_id: str, team_a_name: str, team_b_name: str, avg_skill_a: float, avg_skill_b: float) -> None:
        """Initialize odds for a match based on skill rating difference."""
        # Don't reinitialize if odds already exist in memory
        if match_id in self._odds:
            return

        # When using DB, we'll check in the async version instead
        # This method is for JSON fallback only
        if self._use_db:
            return

        # Calculate initial odds based on skill rating
        # Higher skill team gets lower odds
        # Skill rating is 1.0-10.0, so 1.0 difference = 0.1x odds difference
        skill_diff = avg_skill_b - avg_skill_a
        odds_diff = skill_diff * 0.1  # Changed from 0.5 to 0.1
        base_odds = 1.9

        if skill_diff >= 0:
            # Team B has higher skill -> Team B gets lower odds
            odds_a = base_odds + odds_diff  # Team A gets higher odds
            odds_b = base_odds - odds_diff  # Team B gets lower odds
        else:
            # Team A has higher skill -> Team A gets lower odds
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

    async def initialize_match_odds_db(self, match_id: str, team_a_name: str, team_b_name: str, avg_skill_a: float, avg_skill_b: float) -> None:
        """Initialize odds in database (only if not already initialized)."""
        if not self._use_db:
            return

        from storage.db import get_pool
        import logging
        logger = logging.getLogger(__name__)

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Check if odds already exist
            existing = await conn.fetchrow(
                "SELECT match_id, team_a_odds, team_b_odds, team_a_buffer, team_b_buffer FROM match_odds WHERE match_id = $1",
                match_id
            )
            if existing:
                # Odds already initialized, load into memory and don't reset
                logger.info(f"Odds already exist for {match_id}: team_a={existing['team_a_odds']:.2f}x, team_b={existing['team_b_odds']:.2f}x")
                self._odds[match_id] = MatchOdds(
                    team_a_name=team_a_name,
                    team_b_name=team_b_name,
                    team_a_odds=existing['team_a_odds'],
                    team_b_odds=existing['team_b_odds'],
                    team_a_buffer=existing['team_a_buffer'],
                    team_b_buffer=existing['team_b_buffer']
                )
                return

            # Calculate initial odds based on skill rating
            skill_diff = avg_skill_b - avg_skill_a
            odds_diff = skill_diff * 0.1  # Changed from 0.5 to 0.1
            base_odds = 1.9

            logger.info(f"Initializing odds for {match_id}:")
            logger.info(f"  Team A ({team_a_name}): skill={avg_skill_a:.2f}")
            logger.info(f"  Team B ({team_b_name}): skill={avg_skill_b:.2f}")
            logger.info(f"  Skill diff: {skill_diff:.2f}")
            logger.info(f"  Odds shift: {odds_diff:.2f}x (0.1x per 1.0 skill diff)")

            if skill_diff >= 0:
                odds_a = base_odds + odds_diff
                odds_b = base_odds - odds_diff
                logger.info(f"  Team B has higher skill -> Team B gets lower odds")
            else:
                odds_a = base_odds + odds_diff
                odds_b = base_odds - odds_diff
                logger.info(f"  Team A has higher skill -> Team A gets lower odds")

            odds_a = max(1.1, min(2.7, odds_a))
            odds_b = max(1.1, min(2.7, odds_b))

            logger.info(f"  Final odds: team_a={odds_a:.2f}x, team_b={odds_b:.2f}x")

            # Insert new odds
            await conn.execute(
                """
                INSERT INTO match_odds (match_id, team_a_odds, team_b_odds, team_a_buffer, team_b_buffer)
                VALUES ($1, $2, $3, 0, 0)
                """,
                match_id, odds_a, odds_b
            )

            # Also load into memory
            self._odds[match_id] = MatchOdds(
                team_a_name=team_a_name,
                team_b_name=team_b_name,
                team_a_odds=odds_a,
                team_b_odds=odds_b,
                team_a_buffer=0,
                team_b_buffer=0
            )

    def get_current_odds(self, match_id: str) -> MatchOdds | None:
        """Get current odds for a match."""
        odds = self._odds.get(match_id)
        if odds:
            # Apply limits to ensure odds are within bounds
            odds.team_a_odds = max(1.1, min(2.7, odds.team_a_odds))
            odds.team_b_odds = max(1.1, min(2.7, odds.team_b_odds))
        return odds

    async def get_current_odds_sync(self, match_id: str) -> MatchOdds | None:
        """Get current odds for a match (async version that checks DB)."""
        # Check memory first
        if match_id in self._odds:
            odds = self._odds[match_id]
            odds.team_a_odds = max(1.1, min(2.7, odds.team_a_odds))
            odds.team_b_odds = max(1.1, min(2.7, odds.team_b_odds))
            return odds

        # If using DB and not in memory, try to load from DB
        if self._use_db:
            return await self.get_current_odds_db(match_id)

        return None

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
                        team_a_name="",
                        team_b_name="",
                        team_a_odds=team_a_odds,
                        team_b_odds=team_b_odds,
                        team_a_buffer=row["team_a_buffer"],
                        team_b_buffer=row["team_b_buffer"]
                    )
        return None

    async def save_bet(self, bet: Bet, team_a_name: str, team_b_name: str) -> None:
        """Сохранить ставку с динамическим расчётом коэффициентов."""
        import logging
        logger = logging.getLogger(__name__)

        # Get lock for this match to prevent race conditions
        lock = self._get_lock(bet.match_id)
        async with lock:
            # Ensure odds are initialized
            if bet.match_id not in self._odds:
                logger.warning(f"Odds not in memory for {bet.match_id}, loading from DB...")
                # Try to load from DB first
                if self._use_db:
                    db_odds = await self.get_current_odds_db(bet.match_id)
                    if db_odds:
                        self._odds[bet.match_id] = db_odds
                        logger.info(f"Loaded odds from DB: team_a={db_odds.team_a_odds:.2f}x, team_b={db_odds.team_b_odds:.2f}x")
                    else:
                        # Initialize with default odds if not set
                        logger.warning(f"No odds in DB for {bet.match_id}, initializing with defaults")
                        self._odds[bet.match_id] = MatchOdds(
                            team_a_name=team_a_name,
                            team_b_name=team_b_name,
                            team_a_odds=1.9,
                            team_b_odds=1.9,
                            team_a_buffer=0,
                            team_b_buffer=0
                        )
                else:
                    # Initialize with default odds if not set
                    logger.warning(f"No odds in memory for {bet.match_id}, initializing with defaults")
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

            # Check if user already has a bet (should not happen due to UI check, but double-check)
            existing_bet = await self.get_user_bet(bet.guild_id, bet.user_id, bet.tournament_id, bet.match_id)
            if existing_bet:
                # User already has a bet - reject (should have been caught in UI)
                return

            # New bet, use current odds
            bet.odds = current_bet_odds
            logger.info(f"Saving bet: user={bet.user_name}, match={bet.match_id}, team={bet.team_name}, amount={bet.amount}, odds={current_bet_odds:.2f}x")
            shift_amount = bet.amount

            # Calculate odds shift: 1000 coins = 0.05x shift
            shift_odds = shift_amount / 1000 * 0.05

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
                        remaining_shift = remaining_bet / 1000 * 0.05

                        # Apply shift with floor check
                        new_odds_a = current_odds.team_a_odds - remaining_shift
                        if new_odds_a < 1.1:
                            # Hit floor, remaining goes to buffer
                            overflow = (1.1 - new_odds_a) / 0.05 * 1000
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
                        overflow = (1.1 - new_odds_a) / 0.05 * 1000
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
                        remaining_shift = remaining_bet / 1000 * 0.05

                        # Apply shift with floor check
                        new_odds_b = current_odds.team_b_odds - remaining_shift
                        if new_odds_b < 1.1:
                            # Hit floor, remaining goes to buffer
                            overflow = (1.1 - new_odds_b) / 0.05 * 1000
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
                        overflow = (1.1 - new_odds_b) / 0.05 * 1000
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
                    # (на том же conn — без вложенного pool.acquire)
                    existing = await conn.fetchrow(
                        "SELECT 1 FROM bets WHERE guild_id = $1 AND user_id = $2 AND tournament_id = $3 AND match_id = $4",
                        bet.guild_id, bet.user_id, bet.tournament_id, bet.match_id
                    )
                    if existing:
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

                    # Update odds in DB
                    await conn.execute(
                        """
                        UPDATE match_odds
                        SET team_a_odds = $1, team_b_odds = $2, team_a_buffer = $3, team_b_buffer = $4
                        WHERE match_id = $5
                        """,
                        current_odds.team_a_odds, current_odds.team_b_odds,
                        current_odds.team_a_buffer, current_odds.team_b_buffer,
                        bet.match_id
                    )
                    logger.info(f"Updated odds in DB for {bet.match_id}: team_a={current_odds.team_a_odds:.2f}x, team_b={current_odds.team_b_odds:.2f}x")
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
                logger.info(f"Saved odds to file for {bet.match_id}: team_a={current_odds.team_a_odds:.2f}x, team_b={current_odds.team_b_odds:.2f}x")

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

    async def get_user_bet_in_phase(self, guild_id: int, user_id: int, tournament_id: str, match_type: str) -> Bet | None:
        """Получить ставку пользователя на любой матч в этапе (qualifiers/semifinal/final)."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT guild_id, user_id, user_name, tournament_id, match_id, team_name, team_index, amount, odds FROM bets WHERE guild_id = $1 AND user_id = $2 AND tournament_id = $3 AND match_id LIKE $4",
                    guild_id, user_id, tournament_id, f"{tournament_id}_{match_type}_%"
                )
                if row:
                    return Bet(guild_id=row["guild_id"], user_id=row["user_id"], user_name=row["user_name"], tournament_id=row["tournament_id"], match_id=row["match_id"], team_name=row["team_name"], team_index=row.get("team_index", 0), amount=row["amount"], odds=row["odds"])
                return None
        else:
            # Check all bets for any match in this phase
            for key, bets in self._bets.items():
                if key.startswith(f"{tournament_id}:{match_type}_"):
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
        """Resolve bets for a match and return payouts (user_id -> amount) and payouts by name (user_name -> amount).

        Идемпотентно: расчёт фиксируется в bet_settlements в той же
        транзакции, что и удаление ставок и начисление выигрышей.
        Повторный вызов (или вызов после перезапуска между коммитом
        и выплатой) возвращает пустые payouts — повторного
        начисления не происходит.
        """
        if self._use_db:
            return await self._resolve_match_bets_db(
                guild_id, tournament_id, match_id,
                winning_team_name, winning_team_index,
            )
        return await self._resolve_match_bets_fallback(
            guild_id, tournament_id, match_id,
            winning_team_name, winning_team_index,
        )

    async def _resolve_match_bets_db(
        self, guild_id: int, tournament_id: str, match_id: str,
        winning_team_name: str, winning_team_index: int,
    ) -> tuple[dict[int, int], dict[str, int]]:
        """Атоморный расчёт ставок в PostgreSQL.

        Одна транзакция: проверка bet_settlements → чтение ставок
        (FOR UPDATE) → начисление выигрышей в user_balance →
        запись settlement → удаление ставок. Любой сбой откатывает
        всё — ставка не теряется без выплаты и выплата не
        дублируется.
        """
        import json as _json
        import logging
        from config import MAX_BALANCE
        from storage.db import get_pool
        from storage.betting_stats_store import betting_stats_store

        logger = logging.getLogger(__name__)
        settlement_key = f"{tournament_id}:{match_id}"

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Идемпотентность: settlement уже зафиксирован
                existing = await conn.fetchrow(
                    "SELECT 1 FROM bet_settlements WHERE settlement_key = $1",
                    settlement_key
                )
                if existing:
                    logger.info(
                        "Ставки матча %s уже рассчитаны — пропуск",
                        match_id,
                    )
                    return {}, {}

                rows = await conn.fetch(
                    """
                    SELECT guild_id, user_id, user_name, tournament_id,
                           match_id, team_name, team_index, amount, odds
                    FROM bets
                    WHERE tournament_id = $1 AND match_id = $2
                    FOR UPDATE
                    """,
                    tournament_id, match_id
                )
                if not rows:
                    # Гонка: другой воркер мог закоммитить settlement
                    # между нашей проверкой и SELECT FOR UPDATE.
                    again = await conn.fetchrow(
                        "SELECT 1 FROM bet_settlements WHERE settlement_key = $1",
                        settlement_key
                    )
                    if again:
                        return {}, {}
                    return {}, {}

                bets = [
                    Bet(
                        guild_id=row["guild_id"], user_id=row["user_id"],
                        user_name=row["user_name"],
                        tournament_id=row["tournament_id"],
                        match_id=row["match_id"],
                        team_name=row["team_name"],
                        team_index=row.get("team_index", 0),
                        amount=row["amount"], odds=row["odds"],
                    )
                    for row in rows
                ]

                # Итоговый коэффициент: БД → память → дефолт 1.9
                winning_odds = 1.9
                odds_row = await conn.fetchrow(
                    "SELECT team_a_odds, team_b_odds FROM match_odds WHERE match_id = $1",
                    match_id
                )
                if odds_row:
                    if winning_team_index == 0:
                        winning_odds = odds_row["team_a_odds"]
                    elif winning_team_index == 1:
                        winning_odds = odds_row["team_b_odds"]
                else:
                    mem = self.get_current_odds(match_id)
                    if mem:
                        if winning_team_index == 0:
                            winning_odds = mem.team_a_odds
                        elif winning_team_index == 1:
                            winning_odds = mem.team_b_odds
                winning_odds = max(1.1, min(2.7, winning_odds))

                logger.info(
                    "Resolving bets (DB): tournament_id=%s, match_id=%s, "
                    "winning='%s', index=%s, odds=%.2fx, bets=%d",
                    tournament_id, match_id, winning_team_name,
                    winning_team_index, winning_odds, len(bets),
                )

                payouts: dict[int, int] = {}
                payouts_by_name: dict[str, int] = {}
                for bet in bets:
                    if bet.team_name == winning_team_name:
                        payout = int(bet.amount * winning_odds)
                        payouts[bet.user_id] = payout
                        payouts_by_name[bet.user_name] = payout
                        if payout > 0:
                            # Атомарное начисление с cap MAX_BALANCE —
                            # эквивалент user_balance_store.add_balance.
                            await conn.execute(
                                """
                                INSERT INTO user_balance (guild_id, user_id, balance)
                                VALUES ($1, $2, LEAST(100 + $3, $4))
                                ON CONFLICT (guild_id, user_id)
                                DO UPDATE SET balance =
                                    LEAST(user_balance.balance + $3, $4)
                                """,
                                bet.guild_id, bet.user_id, payout, MAX_BALANCE
                            )
                    else:
                        payouts[bet.user_id] = 0
                        payouts_by_name[bet.user_name] = 0

                # Фиксация расчёта — до удаления ставок, в той же tx
                await conn.execute(
                    """
                    INSERT INTO bet_settlements
                        (settlement_key, guild_id, tournament_id, match_id,
                         winning_team_name, winning_team_index,
                         payouts, payouts_by_name)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
                    """,
                    settlement_key, guild_id, tournament_id, match_id,
                    winning_team_name, winning_team_index,
                    _json.dumps({str(k): v for k, v in payouts.items()}),
                    _json.dumps(payouts_by_name),
                )
                await conn.execute(
                    "DELETE FROM bets WHERE tournament_id = $1 AND match_id = $2",
                    tournament_id, match_id
                )

        # Статистика — после коммита: её сбой не влияет на деньги
        try:
            for bet in bets:
                if bet.team_name == winning_team_name:
                    profit = payouts[bet.user_id] - bet.amount
                    await betting_stats_store.record_bet_result(
                        guild_id, bet.user_id, profit, won=True
                    )
                else:
                    await betting_stats_store.record_bet_result(
                        guild_id, bet.user_id, bet.amount, won=False
                    )
        except Exception as exc:
            logger.error("betting_stats recording failed: %s", exc)

        logger.info("Final payouts (DB): %s", payouts)
        return payouts, payouts_by_name

    async def _resolve_match_bets_fallback(
        self, guild_id: int, tournament_id: str, match_id: str,
        winning_team_name: str, winning_team_index: int,
    ) -> tuple[dict[int, int], dict[str, int]]:
        """JSON-fallback: расчёт в памяти + выплата через balance store.

        Выигрыши начисляются здесь же (единая точка выплаты для
        обоих режимов — вызывающий код не должен начислять повторно).
        """
        from storage.betting_stats_store import betting_stats_store
        from storage.user_balance_store import user_balance_store
        import logging

        bets = await self.get_bets_by_match(tournament_id, match_id)
        payouts = {}
        payouts_by_name = {}

        logging.info(f"Resolving bets for tournament_id={tournament_id}, match_id={match_id}, winning_team_name='{winning_team_name}', winning_team_index={winning_team_index}")
        logging.info(f"Found {len(bets)} bets for this match")

        current_odds = self.get_current_odds(match_id)
        if current_odds:
            if winning_team_index == 0:
                winning_odds = current_odds.team_a_odds
            elif winning_team_index == 1:
                winning_odds = current_odds.team_b_odds
            else:
                winning_odds = 1.9
        else:
            winning_odds = 1.9

        for bet in bets:
            if bet.team_name == winning_team_name:
                payout = int(bet.amount * winning_odds)
                payouts[bet.user_id] = payout
                payouts_by_name[bet.user_name] = payout
                if payout > 0:
                    await user_balance_store.add_balance(
                        bet.guild_id, bet.user_id, payout
                    )
                profit = payout - bet.amount
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, profit, won=True)
            else:
                payouts[bet.user_id] = 0
                payouts_by_name[bet.user_name] = 0
                await betting_stats_store.record_bet_result(guild_id, bet.user_id, bet.amount, won=False)

        await self.delete_bets_by_match(tournament_id, match_id)

        logging.info(f"Final payouts: {payouts}")
        return payouts, payouts_by_name

    def enable_db(self) -> None:
        """Enable database mode."""
        self._use_db = True


# Глобальный экземпляр хранилища
bet_store = BetStore()
