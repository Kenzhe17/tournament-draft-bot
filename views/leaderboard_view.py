"""View для лидерборда с пагинацией."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from storage.player_stats_store import player_stats_store
from utils.embeds import build_leaderboard_embed

if TYPE_CHECKING:
    from bot import TournamentBot


class LeaderboardView(discord.ui.View):
    """View с кнопками пагинации для лидерборда."""

    def __init__(self, guild_id: int, page: int = 1, leaderboard_type: str = "elo"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.page = page
        self.leaderboard_type = leaderboard_type
        self.per_page = 10
        self._total_pages = 0  # Will be set asynchronously

    async def initialize(self) -> None:
        """Initialize total pages asynchronously."""
        self._total_pages = await player_stats_store.get_total_pages(self.guild_id, self.per_page)

        # Always add navigation buttons
        # Previous button
        if self.page > 1:
            self.add_item(
                LeaderboardPageButton(
                    self.guild_id, self.page - 1, "⬅️", discord.ButtonStyle.secondary, self.leaderboard_type
                )
            )
        else:
            # Disabled button for first page
            disabled_button = discord.ui.Button(
                label="⬅️",
                style=discord.ButtonStyle.secondary,
                disabled=True
            )
            self.add_item(disabled_button)

        # Next button
        if self.page < self._total_pages:
            self.add_item(
                LeaderboardPageButton(
                    self.guild_id, self.page + 1, "➡️", discord.ButtonStyle.secondary, self.leaderboard_type
                )
            )
        else:
            # Disabled button for last page
            disabled_button = discord.ui.Button(
                label="➡️",
                style=discord.ButtonStyle.secondary,
                disabled=True
            )
            self.add_item(disabled_button)


class LeaderboardPageButton(discord.ui.Button):
    """Кнопка для перехода на страницу лидерборда."""

    def __init__(self, guild_id: int, page: int, label: str, style: discord.ButtonStyle, leaderboard_type: str = "elo"):
        super().__init__(
            label=label,
            style=style,
            custom_id=f"leaderboard_page:{guild_id}:{page}",
        )
        self.guild_id = guild_id
        self.page = page
        self.leaderboard_type = leaderboard_type

    async def callback(self, interaction: discord.Interaction) -> None:
        total_pages = await player_stats_store.get_total_pages(self.guild_id, 10)

        if self.page < 1 or self.page > total_pages:
            await interaction.response.send_message(
                "❌ Неверная страница.",
                ephemeral=True
            )
            return

        embed = await build_leaderboard_embed(self.guild_id, self.page, self.leaderboard_type)
        view = LeaderboardView(self.guild_id, self.page, self.leaderboard_type)
        await view.initialize()

        await interaction.response.edit_message(embed=embed, view=view)
