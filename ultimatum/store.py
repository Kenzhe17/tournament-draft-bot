"""PostgreSQL store for Ultimatum Solo Tournament System."""

import logging
import uuid
from datetime import datetime
from typing import Optional, List

import asyncpg

from storage.db import get_pool
from ultimatum.models import (
    Tournament, Player, Match, Round, Ranking,
    MatchHistory, AuditLog, Vote,
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
                INSERT INTO ultimatum_tournaments (id, name, guild_id, status, is_open, created_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                tournament_id, name, guild_id, TournamentStatus.DRAFT.value, False, created_by
            )

        return Tournament(
            id=tournament_id,
            name=name,
            guild_id=guild_id,
            status=TournamentStatus.DRAFT,
            is_open=False,
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
                is_open=row.get("is_open", False),
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_by=row["created_by"],
                setup_channel_id=row.get("setup_channel_id"),
                setup_message_id=row.get("setup_message_id"),
                round_channel_id=row.get("round_channel_id"),
                round_message_id=row.get("round_message_id"),
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
                is_open=row.get("is_open", False),
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                created_by=row["created_by"],
                setup_channel_id=row.get("setup_channel_id"),
                setup_message_id=row.get("setup_message_id"),
                round_channel_id=row.get("round_channel_id"),
                round_message_id=row.get("round_message_id"),
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
                    elimination_round=row.get("elimination_round"),  # Use .get() for migration safety
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
                    elimination_round=row.get("elimination_round"),  # Use .get() for migration safety
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
                elimination_round=row.get("elimination_round"),  # Use .get() for migration safety
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

    async def record_vote(
        self,
        match_id: int,
        user_id: int,
        voted_winner_id: int
    ) -> None:
        """Record a vote for match winner."""
        pool = await get_pool()
        vote_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ultimatum_votes (id, match_id, user_id, voted_winner_id)
                VALUES ($1, $2, $3, $4)
                """,
                vote_id, match_id, user_id, voted_winner_id
            )

    async def get_vote(
        self,
        match_id: int,
        user_id: int
    ) -> Optional[Vote]:
        """Get vote by match and user."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM ultimatum_votes
                WHERE match_id = $1 AND user_id = $2
                """,
                match_id, user_id
            )
            if row:
                return Vote(
                    id=row["id"],
                    match_id=row["match_id"],
                    user_id=row["user_id"],
                    voted_winner_id=row["voted_winner_id"],
                    created_at=row["created_at"]
                )
        return None

    async def get_votes(self, match_id: int) -> List[Vote]:
        """Get all votes for a match."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ultimatum_votes
                WHERE match_id = $1
                """,
                match_id
            )
            return [
                Vote(
                    id=row["id"],
                    match_id=row["match_id"],
                    user_id=row["user_id"],
                    voted_winner_id=row["voted_winner_id"],
                    created_at=row["created_at"]
                )
                for row in rows
            ]

    async def clear_votes(self, match_id: int) -> None:
        """Clear all votes for a match."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM ultimatum_votes
                WHERE match_id = $1
                """,
                match_id
            )

    async def set_tournament_open(self, tournament_id: str, is_open: bool) -> None:
        """Set tournament open/closed status."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_tournaments
                SET is_open = $1
                WHERE id = $2
                """,
                is_open, tournament_id
            )

    async def remove_player(self, tournament_id: str, discord_id: int) -> None:
        """Remove player from tournament."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM ultimatum_players
                WHERE tournament_id = $1 AND discord_id = $2
                """,
                tournament_id, discord_id
            )

    # ------------------------------------------------------------------
    # Жизненный цикл старта и ID Discord-сообщений
    # ------------------------------------------------------------------

    async def set_setup_message(
        self, tournament_id: str, channel_id: int, message_id: int
    ) -> None:
        """Запомнить сообщение карточки регистрации."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_tournaments
                SET setup_channel_id = $1, setup_message_id = $2
                WHERE id = $3
                """,
                channel_id, message_id, tournament_id
            )

    async def clear_setup_message(self, tournament_id: str) -> None:
        """Сбросить ссылку на карточку регистрации (после удаления)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_tournaments
                SET setup_channel_id = NULL, setup_message_id = NULL
                WHERE id = $1
                """,
                tournament_id
            )

    async def set_round_channel(self, tournament_id: str, channel_id: int) -> None:
        """Привязать канал карточки раунда (для resync старых турниров)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_tournaments
                SET round_channel_id = $1
                WHERE id = $2
                """,
                channel_id, tournament_id
            )

    async def set_round_message(
        self, tournament_id: str, channel_id: int, message_id: int
    ) -> None:
        """Запомнить сообщение карточки текущего раунда."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ultimatum_tournaments
                SET round_channel_id = $1, round_message_id = $2
                WHERE id = $3
                """,
                channel_id, message_id, tournament_id
            )

    async def begin_tournament(self, tournament_id: str) -> str:
        """Атомарный переход REGISTRATION -> IN_PROGRESS.

        SELECT ... FOR UPDATE на строке турнира сериализует параллельные
        нажатия «Старт». Возвращает:
        - "started"  — переход выполнен этим вызовом;
        - "resumed"  — турнир уже запущен (идемпотентный повтор);
        - "invalid"  — статус не позволяет старт (COMPLETED/CANCELLED/DRAFT).
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status FROM ultimatum_tournaments WHERE id = $1 FOR UPDATE",
                    tournament_id
                )
                if not row:
                    raise ValueError("Tournament not found")
                status = row["status"]
                if status == TournamentStatus.REGISTRATION.value:
                    count = await conn.fetchval(
                        "SELECT COUNT(*) FROM ultimatum_players WHERE tournament_id = $1",
                        tournament_id
                    )
                    if count != 16:
                        raise ValueError(f"Need exactly 16 players, got {count}")
                    await conn.execute(
                        """
                        UPDATE ultimatum_tournaments
                        SET status = $1, current_round = 1,
                            started_at = COALESCE(started_at, NOW())
                        WHERE id = $2
                        """,
                        TournamentStatus.IN_PROGRESS.value, tournament_id
                    )
                    return "started"
                if status in (
                    TournamentStatus.IN_PROGRESS.value,
                    TournamentStatus.ROUND_IN_PROGRESS.value,
                    TournamentStatus.ROUND_COMPLETED.value,
                    TournamentStatus.GRAND_FINAL.value,
                ):
                    return "resumed"
                return "invalid"

    async def round_match_count(self, tournament_id: str, round_number: int) -> int:
        """Количество матчей раунда (для идемпотентных проверок)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM ultimatum_matches
                WHERE tournament_id = $1 AND round_number = $2
                """,
                tournament_id, round_number
            ) or 0

    async def ensure_round_with_matches(
        self,
        tournament_id: str,
        round_number: int,
        pairs: List[tuple],
    ) -> dict:
        """Идемпотентно создать раунд и его матчи в одной транзакции.

        Блокирует строку турнира FOR UPDATE — параллельные вызовы
        сериализуются; внутри повторно проверяется наличие матчей,
        поэтому дубли невозможны. Возвращает
        {"round_created": bool, "matches_created": bool, "match_count": int}.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchrow(
                    "SELECT id FROM ultimatum_tournaments WHERE id = $1 FOR UPDATE",
                    tournament_id
                )
                existing_round = await conn.fetchrow(
                    """
                    SELECT id FROM ultimatum_rounds
                    WHERE tournament_id = $1 AND round_number = $2
                    """,
                    tournament_id, round_number
                )
                round_created = False
                if existing_round is None:
                    await conn.execute(
                        """
                        INSERT INTO ultimatum_rounds (tournament_id, round_number, status)
                        VALUES ($1, $2, $3)
                        """,
                        tournament_id, round_number, RoundStatus.PENDING.value
                    )
                    round_created = True

                match_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM ultimatum_matches
                    WHERE tournament_id = $1 AND round_number = $2
                    """,
                    tournament_id, round_number
                ) or 0
                if match_count > 0:
                    return {
                        "round_created": round_created,
                        "matches_created": False,
                        "match_count": match_count,
                    }

                for p1_id, p2_id in pairs:
                    await conn.execute(
                        """
                        INSERT INTO ultimatum_matches
                        (tournament_id, round_number, player1_id, player2_id,
                         match_type, status)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        tournament_id, round_number, p1_id, p2_id,
                        MatchType.NORMAL.value, MatchStatus.PENDING.value
                    )
                    await conn.execute(
                        """
                        INSERT INTO ultimatum_match_history
                        (tournament_id, player1_id, player2_id, round_number)
                        VALUES ($1, $2, $3, $4)
                        """,
                        tournament_id, p1_id, p2_id, round_number
                    )

                await conn.execute(
                    """
                    UPDATE ultimatum_rounds SET status = $1
                    WHERE tournament_id = $2 AND round_number = $3
                    """,
                    RoundStatus.IN_PROGRESS.value, tournament_id, round_number
                )
                return {
                    "round_created": round_created,
                    "matches_created": True,
                    "match_count": len(pairs),
                }

    async def list_unfinished_tournaments(self) -> List[Tournament]:
        """Все незавершённые турниры (для восстановления после рестарта)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ultimatum_tournaments
                WHERE status IN ($1, $2, $3, $4, $5)
                ORDER BY created_at
                """,
                TournamentStatus.REGISTRATION.value,
                TournamentStatus.IN_PROGRESS.value,
                TournamentStatus.ROUND_IN_PROGRESS.value,
                TournamentStatus.ROUND_COMPLETED.value,
                TournamentStatus.GRAND_FINAL.value,
            )
            return [
                Tournament(
                    id=row["id"],
                    name=row["name"],
                    guild_id=row["guild_id"],
                    status=TournamentStatus(row["status"]),
                    current_round=row["current_round"],
                    is_open=row.get("is_open", False),
                    created_at=row["created_at"],
                    started_at=row["started_at"],
                    completed_at=row["completed_at"],
                    created_by=row["created_by"],
                    setup_channel_id=row.get("setup_channel_id"),
                    setup_message_id=row.get("setup_message_id"),
                    round_channel_id=row.get("round_channel_id"),
                    round_message_id=row.get("round_message_id"),
                )
                for row in rows
            ]


# Global store instance
ultimatum_store = UltimatumStore()
