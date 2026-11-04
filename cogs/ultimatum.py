"""Ultimatum Solo Tournament Cog."""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID
from storage.db import get_pool
from ultimatum.models import TournamentStatus, PlayerStatus, Tournament
from ultimatum.store import ultimatum_store
from ultimatum.tournament import UltimatumTournament
from ultimatum.elo_store import elo_store
from views.ultimatum_view import build_ultimatum_round_embed, build_ultimatum_round_view
from views.ultimatum_setup_view import build_ultimatum_setup_view

logger = logging.getLogger(__name__)


class UltimatumCog(commands.Cog):
    """Cog for Ultimatum Solo Tournament commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tournament_logic = UltimatumTournament(ultimatum_store)

    ultimatum_group = app_commands.Group(name="ultimatum", description="Ultimatum Solo Tournament")

    @ultimatum_group.command(name="create", description="Create new Ultimatum tournament")
    @app_commands.describe(name="Tournament name")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_tournament(
        self,
        interaction: discord.Interaction,
        name: str
    ) -> None:
        """Create a new tournament."""
        # Check for existing active tournament
        existing = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if existing:
            await interaction.response.send_message(
                "❌ Active tournament already exists. Complete or cancel it first.",
                ephemeral=True
            )
            return

        # Create tournament
        tournament = await ultimatum_store.create_tournament(
            name=name,
            guild_id=interaction.guild_id,
            created_by=interaction.user.id
        )

        await interaction.response.send_message(
            f"✅ Tournament '{name}' created! ID: {tournament.id}",
            ephemeral=True
        )

        # Send setup embed with Join button
        embed = self._build_tournament_embed(tournament)
        view = build_ultimatum_setup_view(interaction.guild_id, interaction.channel_id, 0)
        message = await interaction.channel.send(embed=embed, view=view)

        logger.info(f"Tournament created: {tournament.id} by {interaction.user.id}")

    @ultimatum_group.command(name="join", description="Join Ultimatum tournament")
    async def join_tournament(self, interaction: discord.Interaction) -> None:
        """Join the active tournament - like other tournaments."""
        tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ No active tournament found.",
                ephemeral=True
            )
            return

        if tournament.status != TournamentStatus.REGISTRATION:
            await interaction.response.send_message(
                "❌ Tournament is not in registration phase.",
                ephemeral=True
            )
            return

        # Check player count
        players = await ultimatum_store.get_players(tournament.id)
        if len(players) >= 16:
            await interaction.response.send_message(
                "❌ Tournament is full (16 players).",
                ephemeral=True
            )
            return

        # Add player
        try:
            await ultimatum_store.add_player(
                tournament.id,
                interaction.user.id,
                interaction.user.display_name
            )
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {str(e)}",
                ephemeral=True
            )
            return

        players = await ultimatum_store.get_players(tournament.id)
        await interaction.response.send_message(
            f"✅ You joined! Players: {len(players)}/16",
            ephemeral=True
        )

    @ultimatum_group.command(name="test_fill", description="Fill tournament with test players (for testing)")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_fill_tournament(self, interaction: discord.Interaction) -> None:
        """Fill tournament with test players to reach 16."""
        tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ No active tournament found.",
                ephemeral=True
            )
            return

        if tournament.status != TournamentStatus.REGISTRATION:
            await interaction.response.send_message(
                "❌ Tournament is not in registration phase.",
                ephemeral=True
            )
            return

        # Get current players
        players = await ultimatum_store.get_players(tournament.id)
        current_count = len(players)
        needed = 16 - current_count

        if needed <= 0:
            await interaction.response.send_message(
                f"✅ Tournament is already full ({current_count}/16 players).",
                ephemeral=True
            )
            return

        # Add test players
        test_names = [
            "TestPlayer1", "TestPlayer2", "TestPlayer3", "TestPlayer4",
            "TestPlayer5", "TestPlayer6", "TestPlayer7", "TestPlayer8",
            "TestPlayer9", "TestPlayer10", "TestPlayer11", "TestPlayer12",
            "TestPlayer13", "TestPlayer14", "TestPlayer15", "TestPlayer16"
        ]

        added = 0
        for i in range(needed):
            # Use unique fake Discord IDs
            fake_id = 1000000 + i + current_count
            name = test_names[i + current_count] if (i + current_count) < len(test_names) else f"TestPlayer{i + current_count}"

            try:
                await ultimatum_store.add_player(
                    tournament.id,
                    fake_id,
                    name
                )
                added += 1
            except ValueError:
                # Player already exists, skip
                pass

        await interaction.response.send_message(
            f"✅ Added {added} test players. Total: {current_count + added}/16",
            ephemeral=True
        )

        logger.info(f"Test fill: added {added} players to tournament {tournament.id}")

    def _build_tournament_embed(self, tournament) -> discord.Embed:
        """Build tournament status embed."""
        status_emoji = {
            TournamentStatus.DRAFT: "📝",
            TournamentStatus.REGISTRATION: "📋",
            TournamentStatus.READY: "✅",
            TournamentStatus.IN_PROGRESS: "🎮",
            TournamentStatus.ROUND_IN_PROGRESS: "⚔️",
            TournamentStatus.ROUND_COMPLETED: "✅",
            TournamentStatus.GRAND_FINAL: "🏆",
            TournamentStatus.COMPLETED: "🎉",
            TournamentStatus.CANCELLED: "❌",
        }

        emoji = status_emoji.get(tournament.status, "❓")

        embed = discord.Embed(
            title=f"{emoji} УЛЬТИМАТУМ СОЛО: {tournament.name}",
            description=f"Статус: {tournament.status.value}",
            color=discord.Color.gold()
        )

        embed.add_field(
            name="Участники",
            value="0/16",  # Will update with actual count
            inline=True
        )

        embed.add_field(
            name="Текущий раунд",
            value=str(tournament.current_round),
            inline=True
        )

        return embed

    @ultimatum_group.command(name="history", description="View tournament history")
    @app_commands.describe(limit="Number of recent tournaments to show (default: 5)")
    async def tournament_history(
        self,
        interaction: discord.Interaction,
        limit: int = 5
    ) -> None:
        """View tournament history for this guild."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ultimatum_tournaments
                WHERE guild_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                interaction.guild_id, limit
            )

        if not rows:
            await interaction.response.send_message(
                "No tournament history found.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="📜 Tournament History",
            color=discord.Color.blue()
        )

        for row in rows:
            status_emoji = {
                "DRAFT": "📝",
                "REGISTRATION": "📋",
                "READY": "✅",
                "IN_PROGRESS": "🎮",
                "ROUND_IN_PROGRESS": "⚔️",
                "ROUND_COMPLETED": "✅",
                "GRAND_FINAL": "🏆",
                "COMPLETED": "🎉",
                "CANCELLED": "❌",
            }

            emoji = status_emoji.get(row["status"], "❓")
            created_date = row["created_at"].strftime("%Y-%m-%d") if row["created_at"] else "N/A"

            embed.add_field(
                name=f"{emoji} {row['name']}",
                value=f"Status: {row['status']}\nCreated: {created_date}\nRounds: {row['current_round']}",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ultimatum_group.command(name="ranking", description="View final rankings of completed tournament")
    @app_commands.describe(tournament_id="Tournament ID (leave empty for latest completed)")
    async def tournament_ranking(
        self,
        interaction: discord.Interaction,
        tournament_id: Optional[str] = None
    ) -> None:
        """View final rankings of a completed tournament."""
        # Get tournament
        if tournament_id:
            tournament = await ultimatum_store.get_tournament(tournament_id)
        else:
            # Get latest completed tournament
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT * FROM ultimatum_tournaments
                    WHERE guild_id = $1 AND status = $2
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """,
                    interaction.guild_id, TournamentStatus.COMPLETED.value
                )
                if not row:
                    await interaction.response.send_message(
                        "No completed tournaments found.",
                        ephemeral=True
                    )
                    return
                tournament = Tournament(
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

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True
            )
            return

        if tournament.status != TournamentStatus.COMPLETED:
            await interaction.response.send_message(
                "Tournament is not completed yet.",
                ephemeral=True
            )
            return

        # Get players with final places
        players = await ultimatum_store.get_players(tournament.id)
        sorted_players = sorted(players, key=lambda p: p.final_place or 999)

        embed = discord.Embed(
            title=f"🏆 Final Rankings: {tournament.name}",
            description=f"Completed: {tournament.completed_at.strftime('%Y-%m-%d') if tournament.completed_at else 'N/A'}",
            color=discord.Color.gold()
        )

        # Group by ranks for display
        ranking_text = ""
        for player in sorted_players:
            if player.final_place:
                medal = ""
                if player.final_place == 1:
                    medal = "🥇"
                elif player.final_place == 2:
                    medal = "🥈"
                elif player.final_place == 3:
                    medal = "🥉"

                ranking_text += f"{medal} **#{player.final_place}** {player.username} ({player.record})\n"

        embed.add_field(
            name="Final Standings",
            value=ranking_text or "No rankings available",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="tournament_history", description="Show your Ultimatum tournament history")
    async def tournament_history(self, interaction: discord.Interaction) -> None:
        """Show player's tournament match history."""
        tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ No active tournament found.",
                ephemeral=True
            )
            return

        # Get player's matches
        player = await ultimatum_store.get_player_by_discord_id(tournament.id, interaction.user.id)
        if not player:
            await interaction.response.send_message(
                "❌ You're not registered in this tournament.",
                ephemeral=True
            )
            return

        # Get all matches for this tournament
        all_matches = await ultimatum_store.get_matches(tournament.id)

        # Filter matches where player participated
        player_matches = [
            m for m in all_matches
            if m.player1_id == interaction.user.id or m.player2_id == interaction.user.id
        ]

        if not player_matches:
            await interaction.response.send_message(
                "❌ No matches played yet.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📊 История матчей: {player.username}",
            description=f"Записей: {len(player_matches)}",
            color=discord.Color.blue()
        )

        for match in player_matches:
            opponent_id = match.player2_id if match.player1_id == interaction.user.id else match.player1_id
            opponent = await ultimatum_store.get_player_by_discord_id(tournament.id, opponent_id)
            opponent_name = opponent.username if opponent else "Unknown"

            is_winner = match.winner_id == interaction.user.id
            result_emoji = "✅" if is_winner else "❌"

            match_type = "Гранд-финал" if match.match_type == MatchType.GRAND_FINAL else "Раунд"

            embed.add_field(
                name=f"{result_emoji} vs {opponent_name}",
                value=f"{match_type} {match.round_number} | {player.record}",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="tournament_ranking", description="Show current Ultimatum tournament ranking")
    async def tournament_ranking(self, interaction: discord.Interaction) -> None:
        """Show current tournament standings."""
        tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ No active tournament found.",
                ephemeral=True
            )
            return

        # Get all players
        players = await ultimatum_store.get_players(tournament.id)

        # Sort by wins, then by fewer losses
        sorted_players = sorted(
            players,
            key=lambda p: (-p.wins, p.losses if p.losses else 0)
        )

        embed = discord.Embed(
            title=f"🏆 Рейтинг турнира: {tournament.name}",
            description=f"Раунд {tournament.current_round}",
            color=discord.Color.gold()
        )

        for idx, player in enumerate(sorted_players):
            medal = ""
            if idx == 0:
                medal = "🥇 "
            elif idx == 1:
                medal = "🥈 "
            elif idx == 2:
                medal = "🥉 "

            status_emoji = {
                PlayerStatus.CHAMPION: "👑",
                PlayerStatus.GRAND_FINAL_LOSER: "🥈",
                PlayerStatus.GRAND_FINALIST: "⭐",
                PlayerStatus.ELIMINATED: "💀",
                PlayerStatus.ACTIVE: ""
            }.get(player.status, "")

            embed.add_field(
                name=f"{medal}{idx + 1}. {player.username} {status_emoji}",
                value=f"{player.record} | Статус: {player.status.value}",
                inline=False
            )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Register cog."""
    await bot.add_cog(UltimatumCog(bot))
    logger.info("Cog 'ultimatum' loaded")
