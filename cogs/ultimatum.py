"""Ultimatum Solo Tournament Cog."""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID
from ultimatum.models import TournamentStatus, PlayerStatus
from ultimatum.store import ultimatum_store
from ultimatum.tournament import UltimatumTournament

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

        # Send setup embed
        embed = self._build_tournament_embed(tournament)
        view = self._build_setup_view(tournament)
        await interaction.channel.send(embed=embed, view=view)

        logger.info(f"Tournament created: {tournament.id} by {interaction.user.id}")

    @ultimatum_group.command(name="join", description="Join Ultimatum tournament")
    async def join_tournament(self, interaction: discord.Interaction) -> None:
        """Join the active tournament."""
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

        # Update embed
        embed = self._build_tournament_embed(tournament)
        # Update message (need to track message ID)

    @ultimatum_group.command(name="start", description="Start Ultimatum tournament")
    @app_commands.checks.has_permissions(administrator=True)
    async def start_tournament(self, interaction: discord.Interaction) -> None:
        """Start the tournament."""
        tournament = await ultimatum_store.get_active_tournament(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ No active tournament found.",
                ephemeral=True
            )
            return

        players = await ultimatum_store.get_players(tournament.id)
        if len(players) != 16:
            await interaction.response.send_message(
                f"❌ Need exactly 16 players. Current: {len(players)}",
                ephemeral=True
            )
            return

        # Start tournament
        await self.tournament_logic.start_tournament(tournament.id)

        await interaction.response.send_message(
            "✅ Tournament started! Round 1 matches created.",
            ephemeral=True
        )

        # Update embed with round info
        tournament = await ultimatum_store.get_tournament(tournament.id)
        embed = self._build_tournament_embed(tournament)
        matches = await ultimatum_store.get_matches(tournament.id, 1)
        # Update with match info

    def _build_tournament_embed(self, tournament) -> discord.Embed:
        """Build tournament status embed."""
        players = []  # Need to fetch asynchronously

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
        """Build setup buttons view."""
        view = discord.ui.View(timeout=None)

        # Open registration button
        open_reg = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Открыть регистрацию",
            emoji="📋"
        )

        async def open_reg_callback(interaction: discord.Interaction):
            await ultimatum_store.update_tournament_status(
                tournament.id, TournamentStatus.REGISTRATION
            )
            await interaction.response.send_message("✅ Registration opened!", ephemeral=True)

        open_reg.callback = open_reg_callback
        view.add_item(open_reg)

        # Start tournament button
        start_btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Запустить турнир",
            emoji="▶️"
        )

        async def start_callback(interaction: discord.Interaction):
            await self.tournament_logic.start_tournament(tournament.id)
            await interaction.response.send_message("✅ Tournament started!", ephemeral=True)

        start_btn.callback = start_callback
        view.add_item(start_btn)

        return view


async def setup(bot: commands.Bot) -> None:
    """Register cog."""
    await bot.add_cog(UltimatumCog(bot))
    logger.info("Cog 'ultimatum' loaded")
