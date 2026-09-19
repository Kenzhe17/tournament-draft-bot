"""Bets store for betting system."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class Bet:
    """Represents a single bet."""
    id: int | None = None
    guild_id: int = 0
    tournament_id: str = ""
    user_id: int = 0
    match_type: str = ""  # "qualifier", "semifinal", "final"
    match_index: int = 0
    team_index: int = 0
    amount: int = 0
    status: str = "pending"  # "pending", "won", "lost"


class BetsStore:
    """Store for managing bets."""

    def __init__(self) -> None:
        self._use_db = False
        self._bets: list[Bet] = []

    def enable_db(self) -> None:
        """Enable database storage."""
        self._use_db = True

    async def create_bet(
        self,
        guild_id: int,
        tournament_id: str,
        user_id: int,
        match_type: str,
        match_index: int,
        team_index: int,
        amount: int
    ) -> Bet:
        """Create a new bet."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO bets (guild_id, tournament_id, user_id, match_type, match_index, team_index, amount, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
                    RETURNING id
                    """,
                    guild_id, tournament_id, user_id, match_type, match_index, team_index, amount
                )
                return Bet(
                    id=row["id"],
                    guild_id=guild_id,
                    tournament_id=tournament_id,
                    user_id=user_id,
                    match_type=match_type,
                    match_index=match_index,
                    team_index=team_index,
                    amount=amount,
                    status="pending"
                )
        else:
            bet = Bet(
                guild_id=guild_id,
                tournament_id=tournament_id,
                user_id=user_id,
                match_type=match_type,
                match_index=match_index,
                team_index=team_index,
                amount=amount,
                status="pending"
            )
            self._bets.append(bet)
            return bet

    async def get_user_bets(self, guild_id: int, user_id: int, tournament_id: str | None = None) -> list[Bet]:
        """Get all bets for a user."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                if tournament_id:
                    rows = await conn.fetch(
                        """
                        SELECT id, guild_id, tournament_id, user_id, match_type, match_index, team_index, amount, status
                        FROM bets
                        WHERE guild_id = $1 AND user_id = $2 AND tournament_id = $3
                        ORDER BY created_at DESC
                        """,
                        guild_id, user_id, tournament_id
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT id, guild_id, tournament_id, user_id, match_type, match_index, team_index, amount, status
                        FROM bets
                        WHERE guild_id = $1 AND user_id = $2
                        ORDER BY created_at DESC
                        """,
                        guild_id, user_id
                    )
                return [Bet(
                    id=row["id"],
                    guild_id=row["guild_id"],
                    tournament_id=row["tournament_id"],
                    user_id=row["user_id"],
                    match_type=row["match_type"],
                    match_index=row["match_index"],
                    team_index=row["team_index"],
                    amount=row["amount"],
                    status=row["status"]
                ) for row in rows]
        else:
            if tournament_id:
                return [b for b in self._bets if b.guild_id == guild_id and b.user_id == user_id and b.tournament_id == tournament_id]
            return [b for b in self._bets if b.guild_id == guild_id and b.user_id == user_id]

    async def get_match_bets(self, guild_id: int, tournament_id: str, match_type: str, match_index: int) -> list[Bet]:
        """Get all bets for a specific match."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, guild_id, tournament_id, user_id, match_type, match_index, team_index, amount, status
                    FROM bets
                    WHERE guild_id = $1 AND tournament_id = $2 AND match_type = $3 AND match_index = $4 AND status = 'pending'
                    """,
                    guild_id, tournament_id, match_type, match_index
                )
                return [Bet(
                    id=row["id"],
                    guild_id=row["guild_id"],
                    tournament_id=row["tournament_id"],
                    user_id=row["user_id"],
                    match_type=row["match_type"],
                    match_index=row["match_index"],
                    team_index=row["team_index"],
                    amount=row["amount"],
                    status=row["status"]
                ) for row in rows]
        else:
            return [b for b in self._bets 
                    if b.guild_id == guild_id 
                    and b.tournament_id == tournament_id 
                    and b.match_type == match_type 
                    and b.match_index == match_index 
                    and b.status == "pending"]

    async def get_match_bets_by_team(self, guild_id: int, tournament_id: str, match_type: str, match_index: int, team_index: int) -> list[Bet]:
        """Get all bets for a specific team in a match."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, guild_id, tournament_id, user_id, match_type, match_index, team_index, amount, status
                    FROM bets
                    WHERE guild_id = $1 AND tournament_id = $2 AND match_type = $3 AND match_index = $4 AND team_index = $5 AND status = 'pending'
                    """,
                    guild_id, tournament_id, match_type, match_index, team_index
                )
                return [Bet(
                    id=row["id"],
                    guild_id=row["guild_id"],
                    tournament_id=row["tournament_id"],
                    user_id=row["user_id"],
                    match_type=row["match_type"],
                    match_index=row["match_index"],
                    team_index=row["team_index"],
                    amount=row["amount"],
                    status=row["status"]
                ) for row in rows]
        else:
            return [b for b in self._bets 
                    if b.guild_id == guild_id 
                    and b.tournament_id == tournament_id 
                    and b.match_type == match_type 
                    and b.match_index == match_index 
                    and b.team_index == team_index 
                    and b.status == "pending"]

    async def get_total_match_pool(self, guild_id: int, tournament_id: str, match_type: str, match_index: int) -> int:
        """Get total amount bet on a match."""
        bets = await self.get_match_bets(guild_id, tournament_id, match_type, match_index)
        return sum(b.amount for b in bets)

    async def update_bet_status(self, bet_id: int, status: str) -> None:
        """Update bet status."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE bets SET status = $1 WHERE id = $2",
                    status, bet_id
                )
        else:
            for bet in self._bets:
                if bet.id == bet_id:
                    bet.status = status
                    break

    async def resolve_match_bets(self, guild_id: int, tournament_id: str, match_type: str, match_index: int, winner_team_index: int) -> dict[int, int]:
        """Resolve all bets for a match and return payouts per user_id.
        
        Returns: dict mapping user_id to payout amount
        """
        all_bets = await self.get_match_bets(guild_id, tournament_id, match_type, match_index)
        winning_bets = [b for b in all_bets if b.team_index == winner_team_index]
        losing_bets = [b for b in all_bets if b.team_index != winner_team_index]

        total_pool = sum(b.amount for b in all_bets)
        total_winning_bets = sum(b.amount for b in winning_bets)

        payouts: dict[int, int] = {}

        if total_winning_bets > 0:
            for bet in winning_bets:
                # Calculate payout: (bet_amount / total_winning_bets) * total_pool
                payout = int((bet.amount / total_winning_bets) * total_pool)
                payouts[bet.user_id] = payouts.get(bet.user_id, 0) + payout

        # Update bet statuses
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE bets
                    SET status = CASE
                        WHEN team_index = $1 THEN 'won'
                        ELSE 'lost'
                    END
                    WHERE guild_id = $2 AND tournament_id = $3 AND match_type = $4 AND match_index = $5
                    """,
                    winner_team_index, guild_id, tournament_id, match_type, match_index
                )
        else:
            for bet in all_bets:
                bet.status = "won" if bet.team_index == winner_team_index else "lost"

        return payouts


bets_store = BetsStore()
