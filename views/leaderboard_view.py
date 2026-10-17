
"""View для лидерборда с пагинацией."""

from __future__ import annotations

from config import replace_emojis, get_emoji

from typing import TYPE_CHECKING

import discord

from storage.player_stats_store import player_stats_store
from utils.embeds import build_leaderboard_embed

if TYPE_CHECKING:
    from bot import TournamentBot


class TopCategorySelect(discord.ui.Select):
    """Выпадающее меню для переключения категорий лидерборда."""

    def __init__(self, view: "UniversalLeaderboardView"):
        options = [
            discord.SelectOption(
                label="Топ по ELO",
                value="elo",
                description="Турнирный рейтинг и статистика побед",
                emoji="⚔️"
            ),
            discord.SelectOption(
                label="Топ по Монетам",
                value="money",
                description="Самые богатые игроки сервера",
                emoji="💰"
            ),
            discord.SelectOption(
                label="Топ по Уровню",
                value="level",
                description="Опыт аккаунта и прогресс",
                emoji="✨"
            )
        ]
        super().__init__(
            placeholder="Выберите категорию рейтинга...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="leaderboard:select_category"
        )
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        view = self.view_ref
        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы не можете переключать чужие вкладки!"),
                ephemeral=True
            )
            return

        await interaction.response.defer()
        view.leaderboard_type = self.values[0]
        view.page = 1
        view._total_pages = await player_stats_store.get_total_pages(view.guild_id, view.per_page)

        embed = await build_leaderboard_embed(view.guild_id, view.page, view.leaderboard_type, view.user_id)
        view.refresh_buttons()

        await interaction.edit_original_response(embed=embed, view=view)


class UniversalLeaderboardView(discord.ui.View):
    """View с пагинацией и выпадающим меню для лидерборда."""

    def __init__(self, guild_id: int, user_id: int, leaderboard_type: str = "elo"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.author_id = user_id
        self.leaderboard_type = leaderboard_type
        self.page = 1
        self.per_page = 10
        self._total_pages = 0

    async def initialize(self) -> None:
        """Initialize total pages asynchronously."""
        self._total_pages = await player_stats_store.get_total_pages(self.guild_id, self.per_page)

        # Add category select menu
        self.add_item(TopCategorySelect(self))

        # Add navigation buttons
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        """Обновляет состояние кнопок навигации."""
        # Remove existing navigation buttons
        for item in self.children[:]:
            if isinstance(item, discord.ui.Button):
                self.remove_item(item)

        # Previous button
        if self.page > 1:
            self.add_item(
                LeaderboardPageButton(
                    self.guild_id, self.page - 1, "◀", discord.ButtonStyle.primary, self.leaderboard_type, self.user_id
                )
            )
        else:
            disabled_button = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.primary,
                disabled=True
            )
            self.add_item(disabled_button)

        # Next button
        if self.page < self._total_pages:
            self.add_item(
                LeaderboardPageButton(
                    self.guild_id, self.page + 1, "▶", discord.ButtonStyle.primary, self.leaderboard_type, self.user_id
                )
            )
        else:
            disabled_button = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.primary,
                disabled=True
            )
            self.add_item(disabled_button)


class LeaderboardPageButton(discord.ui.Button):
    """Кнопка для перехода на страницу лидерборда."""

    def __init__(self, guild_id: int, page: int, label: str, style: discord.ButtonStyle, leaderboard_type: str = "elo", user_id: int = None):
        super().__init__(
            label=label,
            style=style,
            custom_id=f"leaderboard_page:{guild_id}:{page}:{leaderboard_type}",
        )
        self.guild_id = guild_id
        self.page = page
        self.leaderboard_type = leaderboard_type
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view

        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы не можете перелистывать чужие страницы!"),
                ephemeral=True
            )
            return

        view.page = self.page
        view.leaderboard_type = self.leaderboard_type
        view.refresh_buttons()

        embed = await build_leaderboard_embed(self.guild_id, self.page, self.leaderboard_type, self.user_id)

        await interaction.response.edit_message(embed=embed, view=view)


class LeaderboardView(discord.ui.View):
    """View с кнопками пагинации для лидерборда (deprecated, use UniversalLeaderboardView)."""

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
                    self.guild_id, self.page - 1, "◀", discord.ButtonStyle.primary, self.leaderboard_type
                )
            )
        else:
            # Disabled button for first page
            disabled_button = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.primary,
                disabled=True
            )
            self.add_item(disabled_button)

        # Next button
        if self.page < self._total_pages:
            self.add_item(
                LeaderboardPageButton(
                    self.guild_id, self.page + 1, "▶", discord.ButtonStyle.primary, self.leaderboard_type
                )
            )
        else:
            # Disabled button for last page
            disabled_button = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.primary,
                disabled=True
            )
            self.add_item(disabled_button)
