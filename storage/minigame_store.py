"""Storage for mini-games with betting system."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import List, Optional

from models.minigame import Minigame, MinigameSession
from storage.db import get_pool
from storage.user_balance_store import user_balance_store


class MinigameStore:
    """Store for mini-games."""

    def __init__(self) -> None:
        self._use_db = True
        self._cache: dict[str, Minigame] = {}

    async def get_available_games(self) -> List[Minigame]:
        """Get all available mini-games."""
        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM minigames WHERE is_active = TRUE ORDER BY category, name"
                )
                return [
                    Minigame(
                        id=row["id"],
                        name=row["name"],
                        description=row["description"],
                        category=row["category"],
                        difficulty=row["difficulty"],
                        min_bet=row["min_bet"],
                        max_bet=row["max_bet"],
                        multiplier=row["multiplier"],
                        is_pvp=row["is_pvp"],
                        is_pve=row["is_pve"],
                        is_active=row["is_active"],
                    )
                    for row in rows
                ]
        return []

    async def get_game(self, game_id: str) -> Optional[Minigame]:
        """Get a specific mini-game by ID."""
        if game_id in self._cache:
            return self._cache[game_id]

        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM minigames WHERE id = $1 AND is_active = TRUE", game_id
                )
                if row:
                    game = Minigame(
                        id=row["id"],
                        name=row["name"],
                        description=row["description"],
                        category=row["category"],
                        difficulty=row["difficulty"],
                        min_bet=row["min_bet"],
                        max_bet=row["max_bet"],
                        multiplier=row["multiplier"],
                        is_pvp=row["is_pvp"],
                        is_pve=row["is_pve"],
                        is_active=row["is_active"],
                    )
                    self._cache[game_id] = game
                    return game
        return None

    async def create_session(
        self, guild_id: int, game_id: str, player1_id: int, player1_bet: int
    ) -> Optional[MinigameSession]:
        """Create a new mini-game session."""
        game = await self.get_game(game_id)
        if not game:
            return None

        # Check balance
        balance = await user_balance_store.get_balance(guild_id, player1_id)
        if balance < player1_bet:
            return None

        # Deduct bet
        await user_balance_store.add_balance(guild_id, player1_id, -player1_bet)

        session_id = str(uuid.uuid4())
        session = MinigameSession(
            session_id=session_id,
            game_id=game_id,
            guild_id=guild_id,
            player1_id=player1_id,
            player1_bet=player1_bet,
            player2_id=None,
            player2_bet=None,
            status="waiting",
            created_at=datetime.now(),
            completed_at=None,
            winner_id=None,
            winnings=0,
            payout_processed=False,
        )

        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO minigame_sessions
                    (session_id, game_id, guild_id, player1_id, player1_bet, player2_id, player2_bet, status, created_at, completed_at, winner_id, winnings, payout_processed)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    """,
                    session_id,
                    game_id,
                    guild_id,
                    player1_id,
                    player1_bet,
                    None,
                    None,
                    "waiting",
                    datetime.now(),
                    None,
                    None,
                    0,
                    False,
                )

        return session

    async def join_session(
        self, session_id: str, player2_id: int, player2_bet: int
    ) -> Optional[MinigameSession]:
        """Join an existing PvP session."""
        session = await self.get_session(session_id)
        if not session or session.status != "waiting":
            return None

        # Check balance
        balance = await user_balance_store.get_balance(session.guild_id, player2_id)
        if balance < player2_bet:
            return None

        # Deduct bet
        await user_balance_store.add_balance(session.guild_id, player2_id, -player2_bet)

        session.player2_id = player2_id
        session.player2_bet = player2_bet
        session.status = "active"

        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE minigame_sessions
                    SET player2_id = $1, player2_bet = $2, status = 'active'
                    WHERE session_id = $3
                    """,
                    player2_id,
                    player2_bet,
                    session_id,
                )

        return session

    async def get_session(self, session_id: str) -> Optional[MinigameSession]:
        """Get a session by ID."""
        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM minigame_sessions WHERE session_id = $1", session_id
                )
                if row:
                    return MinigameSession(
                        session_id=row["session_id"],
                        game_id=row["game_id"],
                        guild_id=row["guild_id"],
                        player1_id=row["player1_id"],
                        player1_bet=row["player1_bet"],
                        player2_id=row["player2_id"],
                        player2_bet=row["player2_bet"],
                        status=row["status"],
                        created_at=row["created_at"],
                        completed_at=row["completed_at"],
                        winner_id=row["winner_id"],
                        winnings=row["winnings"],
                        payout_processed=row["payout_processed"],
                    )
        return None

    async def update_session(self, session: MinigameSession) -> None:
        """Update a session."""
        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE minigame_sessions
                    SET status = $1, completed_at = $2, winner_id = $3, winnings = $4, payout_processed = $5
                    WHERE session_id = $6
                    """,
                    session.status,
                    session.completed_at,
                    session.winner_id,
                    session.winnings,
                    session.payout_processed,
                    session.session_id,
                )

    async def complete_session(
        self, session_id: str, winner_id: int, winnings: int
    ) -> Optional[MinigameSession]:
        """Complete a session and calculate winnings."""
        session = await self.get_session(session_id)
        if not session:
            return None

        session.status = "completed"
        session.completed_at = datetime.now()
        session.winner_id = winner_id
        session.winnings = winnings

        await self.update_session(session)
        await self.process_payout(session_id)

        return session

    async def process_payout(self, session_id: str) -> bool:
        """Process payout for a completed session."""
        session = await self.get_session(session_id)
        if not session or session.payout_processed:
            return False

        # Add winnings to winner
        if session.winner_id and session.winnings > 0:
            await user_balance_store.add_balance(
                session.guild_id, session.winner_id, session.winnings
            )

        # Update stats
        await self.update_player_stats(session)

        # Mark as processed
        session.payout_processed = True
        await self.update_session(session)

        return True

    async def update_player_stats(self, session: MinigameSession) -> None:
        """Update player statistics after a game."""
        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Update player1 stats
                await conn.execute(
                    """
                    INSERT INTO minigame_stats (guild_id, user_id, game_id, games_played, games_won, total_bet, total_won, net_profit)
                    VALUES ($1, $2, $3, 1, $4, $5, $6, $7)
                    ON CONFLICT (guild_id, user_id, game_id)
                    DO UPDATE SET
                        games_played = minigame_stats.games_played + 1,
                        games_won = minigame_stats.games_won + $4,
                        total_bet = minigame_stats.total_bet + $5,
                        total_won = minigame_stats.total_won + $6,
                        net_profit = minigame_stats.net_profit + $7
                    """,
                    session.guild_id,
                    session.player1_id,
                    session.game_id,
                    1 if session.winner_id == session.player1_id else 0,
                    session.player1_bet,
                    session.winnings if session.winner_id == session.player1_id else 0,
                    (session.winnings if session.winner_id == session.player1_id else 0) - session.player1_bet,
                )

                # Update player2 stats if exists
                if session.player2_id:
                    await conn.execute(
                        """
                        INSERT INTO minigame_stats (guild_id, user_id, game_id, games_played, games_won, total_bet, total_won, net_profit)
                        VALUES ($1, $2, $3, 1, $4, $5, $6, $7)
                        ON CONFLICT (guild_id, user_id, game_id)
                        DO UPDATE SET
                            games_played = minigame_stats.games_played + 1,
                            games_won = minigame_stats.games_won + $4,
                            total_bet = minigame_stats.total_bet + $5,
                            total_won = minigame_stats.total_won + $6,
                            net_profit = minigame_stats.net_profit + $7
                        """,
                        session.guild_id,
                        session.player2_id,
                        session.game_id,
                        1 if session.winner_id == session.player2_id else 0,
                        session.player2_bet if session.player2_bet else 0,
                        session.winnings if session.winner_id == session.player2_id else 0,
                        (session.winnings if session.winner_id == session.player2_id else 0) - (session.player2_bet if session.player2_bet else 0),
                    )

    async def get_player_stats(
        self, guild_id: int, user_id: int
    ) -> List[dict]:
        """Get player statistics for all games."""
        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT * FROM minigame_stats
                    WHERE guild_id = $1 AND user_id = $2
                    ORDER BY total_won DESC
                    """,
                    guild_id,
                    user_id,
                )
                return [dict(row) for row in rows]
        return []

    async def get_leaderboard(self, guild_id: int, limit: int = 10) -> List[dict]:
        """Get leaderboard by net profit."""
        if self._use_db:
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT user_id, SUM(net_profit) as total_profit, SUM(games_played) as total_games, SUM(games_won) as total_wins
                    FROM minigame_stats
                    WHERE guild_id = $1
                    GROUP BY user_id
                    ORDER BY total_profit DESC
                    LIMIT $2
                    """,
                    guild_id,
                    limit,
                )
                return [dict(row) for row in rows]
        return []


minigame_store = MinigameStore()
