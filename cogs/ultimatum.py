"""Ultimatum Solo Tournament Cog."""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, replace_emojis
from ultimatum.models import TournamentStatus, PlayerStatus
from ultimatum.store import ultimatum_store
from ultimatum.tournament import UltimatumTournament
from ultimatum.elo_store import elo_store
from views.ultimatum_view import build_ultimatum_round_embed, UltimatumMatchView

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
        view = self._build_setup_view(tournament)
        message = await interaction.channel.send(embed=embed, view=view)

        # Store message ID for updates
        # (need to add this to tournament model)

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

    def _build_setup_view(self, tournament) -> discord.ui.View:
        """Build setup buttons view - like other tournaments."""
        view = discord.ui.View(timeout=None)

        # Join button (like other tournaments)
        join_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Войти",
            emoji="✅"
        )

        async def join_callback(interaction: discord.Interaction):
            # Same logic as /ultimatum join
            tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
            if not tournament or tournament.status != TournamentStatus.REGISTRATION:
                await interaction.response.send_message("❌ Registration not open", ephemeral=True)
                return

            players = await ultimatum_store.get_players(tournament.id)
            if len(players) >= 16:
                await interaction.response.send_message("❌ Tournament full", ephemeral=True)
                return

            try:
                await ultimatum_store.add_player(
                    tournament.id, interaction.user.id, interaction.user.display_name
                )
                await interaction.response.send_message("✅ Joined!", ephemeral=True)
            except ValueError:
                await interaction.response.send_message("❌ Already joined", ephemeral=True)

        join_btn.callback = join_callback
        view.add_item(join_btn)

        # Start button (like other tournaments)
        start_btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Старт",
            emoji="▶️"
        )

        async def start_callback(interaction: discord.Interaction):
            tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
            if not tournament:
                await interaction.response.send_message("❌ No tournament", ephemeral=True)
                return

            players = await ultimatum_store.get_players(tournament.id)
            if len(players) != 16:
                await interaction.response.send_message(f"❌ Need 16 players, have {len(players)}", ephemeral=True)
                return

            await self.tournament_logic.start_tournament(tournament.id)
            await interaction.response.send_message("✅ Tournament started!", ephemeral=True)

            # Send round embed with matches
            tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
            if tournament:
                embed = await build_ultimatum_round_embed(tournament.id, 1)
                view = UltimatumMatchView(self.bot, tournament.id, 1)
                await view.initialize()
                await interaction.channel.send(embed=embed, view=view)

        start_btn.callback = start_callback
        view.add_item(start_btn)

        return view


async def setup(bot: commands.Bot) -> None:
    """Register cog."""
    await bot.add_cog(UltimatumCog(bot))
    logger.info("Cog 'ultimatum' loaded")
