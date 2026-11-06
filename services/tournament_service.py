"""Tournament service for business logic."""

from __future__ import annotations

import logging
import time
from typing import Optional

from core.models import (
    TournamentState,
    TournamentPhase,
    TournamentSize,
    FormationMode,
    RegistrationState,
    Team,
    Match,
)
from storage.db import get_pool

logger = logging.getLogger(__name__)


class TournamentService:
    """Service for tournament business logic."""

    @staticmethod
    async def create_tournament(
        guild_id: int,
        channel_id: int,
        size: TournamentSize = TournamentSize.EIGHT,
        formation_mode: FormationMode = FormationMode.MANUAL,
        organizer_id: int = 0,
    ) -> TournamentState:
        """Create a new tournament."""
        tournament_id = f"{guild_id}_{channel_id}_{int(time.time())}"

        tournament = TournamentState(
            tournament_id=tournament_id,
            guild_id=guild_id,
            channel_id=channel_id,
            size=size,
            formation_mode=formation_mode,
            organizer_id=organizer_id,
            created_at=time.time(),
        )

        # Save to database
        await TournamentService._save_tournament(tournament)

        logger.info("Created tournament %s in channel %s", tournament_id, channel_id)
        return tournament

    @staticmethod
    async def get_tournament(guild_id: int, channel_id: int) -> Optional[TournamentState]:
        """Get tournament by guild and channel."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tournaments WHERE guild_id = $1 AND channel_id = $2",
                guild_id, channel_id
            )

            if not row:
                return None

            # Load tournament state
            tournament = TournamentState(
                tournament_id=row["tournament_id"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                phase=TournamentPhase(row["phase"]),
                size=TournamentSize(row["size"]),
                formation_mode=FormationMode(row["formation_mode"]),
                registration_state=RegistrationState(row["registration_state"]),
                organizer_id=row["organizer_id"],
                created_at=row["created_at"],
                is_active=row["is_active"],
            )

            # Load teams
            team_rows = await conn.fetch(
                "SELECT * FROM teams WHERE tournament_id = $1",
                tournament.tournament_id
            )
            for team_row in team_rows:
                team = Team(
                    team_id=team_row["team_id"],
                    name=team_row["name"],
                    is_complete=team_row["is_complete"],
                )

                # Load team players
                player_rows = await conn.fetch(
                    "SELECT player_id FROM team_players WHERE tournament_id = $1 AND team_id = $2",
                    tournament.tournament_id, team.team_id
                )
                team.players = [row["player_id"] for row in player_rows]

                tournament.teams[team.team_id] = team

            # Load matches
            match_rows = await conn.fetch(
                "SELECT * FROM matches WHERE tournament_id = $1",
                tournament.tournament_id
            )
            for match_row in match_rows:
                match = Match(
                    match_id=match_row["match_id"],
                    team1_id=match_row["team1_id"],
                    team2_id=match_row["team2_id"],
                    winner_id=match_row["winner_id"],
                    is_completed=match_row["is_completed"],
                    stats_submitted=match_row["stats_submitted"],
                )
                tournament.matches[match.match_id] = match

            return tournament

    @staticmethod
    async def update_tournament(tournament: TournamentState) -> None:
        """Update tournament state."""
        await TournamentService._save_tournament(tournament)

    @staticmethod
    async def delete_tournament(guild_id: int, channel_id: int) -> None:
        """Delete tournament."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Get tournament_id first
            row = await conn.fetchrow(
                "SELECT tournament_id FROM tournaments WHERE guild_id = $1 AND channel_id = $2",
                guild_id, channel_id
            )
            if row:
                tournament_id = row["tournament_id"]
                # Delete related data (cascade will handle teams, matches, etc.)
                await conn.execute(
                    "DELETE FROM tournaments WHERE tournament_id = $1",
                    tournament_id
                )
                logger.info("Deleted tournament %s", tournament_id)

    @staticmethod
    async def _save_tournament(tournament: TournamentState) -> None:
        """Save tournament to database."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Upsert tournament
            await conn.execute(
                """
                INSERT INTO tournaments (
                    tournament_id, guild_id, channel_id, phase, size,
                    formation_mode, registration_state, organizer_id,
                    created_at, is_active
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (tournament_id) DO UPDATE SET
                    phase = EXCLUDED.phase,
                    size = EXCLUDED.size,
                    formation_mode = EXCLUDED.formation_mode,
                    registration_state = EXCLUDED.registration_state,
                    organizer_id = EXCLUDED.organizer_id,
                    created_at = EXCLUDED.created_at,
                    is_active = EXCLUDED.is_active
                """,
                tournament.tournament_id,
                tournament.guild_id,
                tournament.channel_id,
                tournament.phase.value,
                tournament.size.value,
                tournament.formation_mode.value,
                tournament.registration_state.value,
                tournament.organizer_id,
                tournament.created_at,
                tournament.is_active,
            )

            # Save teams
            for team in tournament.teams.values():
                await conn.execute(
                    """
                    INSERT INTO teams (team_id, tournament_id, name, is_complete)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (tournament_id, team_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        is_complete = EXCLUDED.is_complete
                    """,
                    team.team_id,
                    tournament.tournament_id,
                    team.name,
                    team.is_complete,
                )

                # Save team players
                for player_id in team.players:
                    await conn.execute(
                        """
                        INSERT INTO team_players (tournament_id, team_id, player_id)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (tournament_id, team_id, player_id) DO NOTHING
                        """,
                        tournament.tournament_id,
                        team.team_id,
                        player_id,
                    )

            # Save matches
            for match in tournament.matches.values():
                await conn.execute(
                    """
                    INSERT INTO matches (
                        match_id, tournament_id, team1_id, team2_id,
                        winner_id, is_completed, stats_submitted, match_type
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (match_id) DO UPDATE SET
                        team1_id = EXCLUDED.team1_id,
                        team2_id = EXCLUDED.team2_id,
                        winner_id = EXCLUDED.winner_id,
                        is_completed = EXCLUDED.is_completed,
                        stats_submitted = EXCLUDED.stats_submitted,
                        match_type = EXCLUDED.match_type
                    """,
                    match.match_id,
                    tournament.tournament_id,
                    match.team1_id,
                    match.team2_id,
                    match.winner_id,
                    match.is_completed,
                    match.stats_submitted,
                    match.match_id.split("_")[-1],  # Extract match type from ID
                )

    @staticmethod
    def add_player(tournament: TournamentState, player_id: int, player_name: str) -> bool:
        """Add player to tournament."""
        if tournament.is_player_registered(player_id):
            return False

        tournament.player_ids.append(player_id)
        tournament.player_names[player_id] = player_name
        return True

    @staticmethod
    def remove_player(tournament: TournamentState, player_id: int) -> bool:
        """Remove player from tournament."""
        if not tournament.is_player_registered(player_id):
            return False

        tournament.player_ids.remove(player_id)
        tournament.player_names.pop(player_id, None)

        # Remove from team if in one
        team = tournament.get_team_by_player(player_id)
        if team:
            team.players.remove(player_id)
            if len(team.players) < 4:
                team.is_complete = False

        return True

    @staticmethod
    def create_teams(tournament: TournamentState, teams_data: list[dict]) -> None:
        """Create teams from data."""
        tournament.teams.clear()

        for i, team_data in enumerate(teams_data):
            team = Team(
                team_id=i + 1,
                name=team_data.get("name", f"Team {i + 1}"),
                players=team_data.get("players", []),
                is_complete=len(team_data.get("players", [])) == 4,
            )
            tournament.teams[team.team_id] = team

    @staticmethod
    def create_matches(tournament: TournamentState) -> None:
        """Create matches based on tournament phase."""
        tournament.matches.clear()

        if tournament.phase == TournamentPhase.QUALIFIERS:
            # Create qualifier matches
            team_ids = list(tournament.teams.keys())
            for i in range(0, len(team_ids), 2):
                if i + 1 < len(team_ids):
                    match_id = f"{tournament.tournament_id}_qualifier_{i // 2}"
                    match = Match(
                        match_id=match_id,
                        team1_id=team_ids[i],
                        team2_id=team_ids[i + 1],
                    )
                    tournament.matches[match_id] = match

        elif tournament.phase == TournamentPhase.SEMIFINALS:
            # Create semifinal matches from qualifier winners
            # TODO: Implement based on qualifier results
            pass

        elif tournament.phase == TournamentPhase.FINAL:
            # Create final match
            # TODO: Implement based on semifinal results
            pass

    @staticmethod
    def confirm_match_winner(
        tournament: TournamentState,
        match_id: str,
        winning_team_id: int,
    ) -> bool:
        """Confirm match winner and update tournament state."""
        match = tournament.matches.get(match_id)
        if not match or match.is_completed:
            return False

        match.winner_id = winning_team_id
        match.is_completed = True
        match.stats_submitted = True

        # Update tournament phase if needed
        # TODO: Implement phase transitions

        return True

    @staticmethod
    async def get_active_tournaments(guild_id: int) -> list[TournamentState]:
        """Get all active tournaments for guild."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tournament_id, channel_id FROM tournaments WHERE guild_id = $1 AND is_active = TRUE",
                guild_id
            )

            tournaments = []
            for row in rows:
                tournament = await TournamentService.get_tournament(guild_id, row["channel_id"])
                if tournament:
                    tournaments.append(tournament)

            return tournaments
