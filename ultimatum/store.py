"""PostgreSQL store for Ultimatum Solo Tournament System."""

import logging
import uuid
from datetime import datetime
from typing import Optional, List

import asyncpg

from storage.db import get_pool
from ultimatum.models import (
    Tournament, Player, Match, Round, Ranking,
    MatchHistory, AuditLog,
    TournamentStatus, PlayerStatus, MatchStatus, MatchType, RoundStatus
)

logger = logging.getLogger(__name__)


class UltimatumStore:
    """Store for Ultimatum tournament data."""

    async def create_tournament(
        self,
        name: str,
        guild_id: int,
        created_by: int
    ) -> Tournament:
        """Create a new tournament."""
        pool = await get_pool()
        tournament_id = str(uuid.uuid4())

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ultimatum_tournaments (id, name, guild_id, status, created_by)
                VALUES ($1, $2, $3, $4, $5)
                """,
                tournament_id, name, guild_id, TournamentStatus.DRAFT.value, created_by
            )

        return Tournament(
            id=tournament_id,
            name=name,
            guild_id=guild_id,
            status=TournamentStatus.DRAFT,
            created_by=created_by,
            created_at=datetime.now()
        )

    async def get_tournament(self, tournament_id: str) -> Optional[Tournament]:
        """Get tournament by ID."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ultimatum_tournaments WHERE id = $1",
                tournament_id
            )
            if not row:
                return None
            return Tournament(
                id=row["id"],
                name=row["name"],
                guild_id=row["guild_id"],
                status=TournamentStatus(row["status"]),
                current_round=row["current_round"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_by=row["created_by"],
            )

    async def get_active_tournament(self, guild_id: int) -> Optional[Tournament]:
        """Get active tournament for guild."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM ultimatum_tournaments
                WHERE guild_id = $1 AND status IN ($2, $3, $4, $5, $6, $7)
                ORDER BY created_at DESC LIMIT 1
                """,
                guild_id,
                TournamentStatus.REGISTRATION.value,
                TournamentStatus.READY.value,
                TournamentStatus.IN_PROGRESS.value,
                TournamentStatus.ROUND_IN_PROGRESS.value,
                TournamentStatus.ROUND_COMPLETED.value,
                TournamentStatus.GRAND_FINAL.value,
            )
            if not row:
                return None
            return Tournament(
                id=row["id"],
                name=row["name"],
                guild_id=row["guild_id"],
                status=TournamentStatus(row["status"]),
                current_round=row["current_round"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_by=row["created_by"],
            )

    async def update_tournament_status(
        self,
        tournament_id: str,
        status: TournamentStatus,
        current_round: Optional[int] = None
    ) -> None:
        """Update tournament status."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if current_round is not None:
                await conn.execute(
                    """
                    UPDATE ultimatum_tournaments
                    SET status = $1, current_round = $2
                    WHERE id = $3
                    """,
                    status.value, current_round, tournament_id
                )
            else:
                await conn.execute(
                    """
                    UPDATE ultimatum_tournaments
                    SET status = $1
                    WHERE id = $2
                    """,
                    status.value, tournament_id
                )

    async def add_player(
        self,
        tournament_id: str,
        discord_id: int,
        username: str
    ) -> Player:
        """Add player to tournament."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO ultimatum_players (tournament_id, discord_id, username, wins, losses, status)
                    VALUES ($1, $2, $3, 0, 0, $4)
                    """,
                    tournament_id, discord_id, username, PlayerStatus.ACTIVE.value
                )
            except asyncpg.UniqueViolationError:
                raise ValueError("Player already registered in this tournament")

        return Player(
            tournament_id=tournament_id,
            discord_id=discord_id,
            username=username,
            wins=0,
            losses=0,
            status=PlayerStatus.ACTIVE,
            created_at=datetime.now()
        )

    async def get_players(self, tournament_id: str) -> List[Player]:
        """Get all players in tournament."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ultimatum_players
                WHERE tournament_id = $1
                ORDER BY id
                """,
                tournament_id
            )
            return [
                Player(
                    id=row["id"],
                    tournament_id=row["tournament_id"],
                    discord_id=row["discord_id"],
                    username=row["username"],
                    wins=row["wins"],
                    losses=row["losses"],
                    status=PlayerStatus(row["status"]),
                    final_place=row["final_place"],
                    elimination_round=row["elimination_round"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_active_players(self, tournament_id: str) -> List[Player]:
        """Get active (not eliminated) players."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ultimatum_players
                WHERE tournament_id = $1 AND status = $2
                ORDER BY id
                """,
                tournament_id, PlayerStatus.ACTIVE.value
            )
            return [
                Player(
                    id=row["id"],
                    tournament_id=row["tournament_id"],
                    discord_id=row["discord_id"],
                    username=row["username"],
                    wins=row["wins"],
                    losses=row["losses"],
                    status=PlayerStatus(row["status"]),
                    final_place=row["final_place"],
                    elimination_round=row["elimination_round"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def get_player_by_discord_id(
        self,
        tournament_id: str,
        discord_id: int
    ) -> Optional[Player]:
        """Get player by Discord ID."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM ultimatum_players
                WHERE tournament_id = $1 AND discord_id = $2
                """,
                tournament_id, discord_id
            )
            if not row:
                return None
            return Player(
                id=row["id"],
                tournament_id=row["tournament_id"],
                discord_id=row["discord_id"],
                username=row["username"],
                wins=row["wins"],
                losses=row["losses"],
                status=PlayerStatus(row["status"]),
                final_place=row["final_place"],
                elimination_round=row["elimination_round"],
                created_at=row["created_at"],
            )

    async def update_player_stats(
        self,
        tournament_id: str,
        discord_id: int,
        wins: int,
        losses: int,
        status: Optional[PlayerStatus] = None,
        elimination_round: Optional[int] = None
    ) -> None:
        """Update player stats."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if status:
                if elimination_round is not None:
                    await conn.execute(
                        """
                        UPDATE ultimatum_players
                        SET wins = $1, losses = $2, status = $3, elimination_round = $4
                        WHERE tournament_id = $5 AND discord_id = $6
                        """,
                        wins, losses, status.value, elimination_round, tournament_id, discord_id
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE ultimatum_players
                        SET wins = $1, losses = $2, status = $3
                        WHERE tournament_id = $4 AND discord_id = $5
                        """,
                        wins, losses, status.value, tournament_id, discord_id
                    )
            else:
                await conn.execute(
                    """
                    UPDATE ultimatum_players
                    SET wins = $1, losses = $2
                    WHERE tournament_id = $3 AND discord_id = $4
                    """,
                    wins, losses, tournament_id, discord_id
                )

    async def update_player_final_place(
        self,
        tournament_id: str,
        discord_id: int,
        final_place: int
    ) -> None:
        """Update player's final place in tournament."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_players
                SET final_place = $1
                WHERE tournament_id = $2 AND discord_id = $3
                """,
                final_place, tournament_id, discord_id
            )

    async def create_match(
        self,
        tournament_id: str,
        round_number: int,
        player1_id: int,
        player2_id: int,
        match_type: MatchType = MatchType.NORMAL
    ) -> Match:
        """Create a match."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ultimatum_matches
                (tournament_id, round_number, player1_id, player2_id, match_type, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                tournament_id, round_number, player1_id, player2_id,
                match_type.value, MatchStatus.PENDING.value
            )

        return Match(
            id=row["id"],
            tournament_id=tournament_id,
            round_number=round_number,
            player1_id=player1_id,
            player2_id=player2_id,
            match_type=match_type,
            status=MatchStatus.PENDING,
            created_at=datetime.now()
        )

    async def get_matches(self, tournament_id: str, round_number: Optional[int] = None) -> List[Match]:
        """Get matches for tournament."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if round_number:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ultimatum_matches
                    WHERE tournament_id = $1 AND round_number = $2
                    ORDER BY id
                    """,
                    tournament_id, round_number
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ultimatum_matches
                    WHERE tournament_id = $1
                    ORDER BY round_number, id
                    """,
                    tournament_id
                )
            return [
                Match(
                    id=row["id"],
                    tournament_id=row["tournament_id"],
                    round_number=row["round_number"],
                    player1_id=row["player1_id"],
                    player2_id=row["player2_id"],
                    winner_id=row["winner_id"],
                    loser_id=row["loser_id"],
                    status=MatchStatus(row["status"]),
                    match_type=MatchType(row["match_type"]),
                    room_id=row["room_id"],
                    room_password=row["room_password"],
                    created_at=row["created_at"],
                    completed_at=row["completed_at"],
                )
                for row in rows
            ]

    async def complete_match(
        self,
        match_id: int,
        winner_id: int,
        loser_id: int
    ) -> None:
        """Complete a match."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_matches
                SET winner_id = $1, loser_id = $2, status = $3, completed_at = $4
                WHERE id = $5
                """,
                winner_id, loser_id, MatchStatus.COMPLETED.value,
                datetime.now(), match_id
            )

    async def get_match_by_id(self, match_id: int) -> Optional[Match]:
        """Get match by database ID."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ultimatum_matches WHERE id = $1",
                match_id
            )
            if not row:
                return None
            return Match(
                id=row["id"],
                tournament_id=row["tournament_id"],
                round_number=row["round_number"],
                player1_id=row["player1_id"],
                player2_id=row["player2_id"],
                winner_id=row["winner_id"],
                loser_id=row["loser_id"],
                status=MatchStatus(row["status"]),
                match_type=MatchType(row["match_type"]),
                room_id=row["room_id"],
                room_password=row["room_password"],
                created_at=row["created_at"],
                completed_at=row["completed_at"],
            )

    async def update_match_room(
        self,
        match_id: int,
        room_id: str,
        room_password: str
    ) -> None:
        """Update match with room data."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_matches
                SET room_id = $1, room_password = $2
                WHERE id = $3
                """,
                room_id, room_password, match_id
            )

    async def create_round(self, tournament_id: str, round_number: int) -> Round:
        """Create a round."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ultimatum_rounds (tournament_id, round_number, status)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                tournament_id, round_number, RoundStatus.PENDING.value
            )

        return Round(
            id=row["id"],
            tournament_id=tournament_id,
            round_number=round_number,
            status=RoundStatus.PENDING,
            created_at=datetime.now()
        )

    async def update_round_status(
        self,
        tournament_id: str,
        round_number: int,
        status: RoundStatus
    ) -> None:
        """Update round status."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_rounds
                SET status = $1, completed_at = $2
                WHERE tournament_id = $3 AND round_number = $4
                """,
                status.value,
                datetime.now() if status == RoundStatus.COMPLETED else None,
                tournament_id, round_number
            )

    async def add_match_history(
        self,
        tournament_id: str,
        player1_id: int,
        player2_id: int,
        round_number: int
    ) -> None:
        """Add match to history."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ultimatum_match_history
                (tournament_id, player1_id, player2_id, round_number)
                VALUES ($1, $2, $3, $4)
                """,
                tournament_id, player1_id, player2_id, round_number
            )

    async def have_played_before(
        self,
        tournament_id: str,
        player1_id: int,
        player2_id: int
    ) -> bool:
        """Check if two players have played before."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM ultimatum_match_history
                WHERE tournament_id = $1
                AND ((player1_id = $2 AND player2_id = $3) OR (player1_id = $3 AND player2_id = $2))
                """,
                tournament_id, player1_id, player2_id
            )
            return count > 0

    async def log_audit(
        self,
        tournament_id: str,
        actor_discord_id: int,
        action: str,
        details: Optional[dict] = None
    ) -> None:
        """Log audit entry."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ultimatum_audit_log
                (tournament_id, actor_discord_id, action, details)
                VALUES ($1, $2, $3, $4)
                """,
                tournament_id, actor_discord_id, action, details
            )


# Global store instance
ultimatum_store = UltimatumStore()
