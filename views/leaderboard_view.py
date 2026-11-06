"""View для лидерборда с пагинацией (Components V2)."""

from __future__ import annotations

from config import replace_emojis, get_emoji

from typing import TYPE_CHECKING

import discord

from storage.player_stats_store import player_stats_store
from utils.embeds import build_leaderboard_embed
from utils.design.cv2 import container_from_embed

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
                label="Топ по Ultimatum Elo",
                value="ulti_elo",
                description="Рейтинг Ultimatum 1v1",
                emoji="🎮"
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
        await view._render()

        await interaction.edit_original_response(view=view)


class _NavButton(discord.ui.Button):
    """Кнопка навигации лидерборда (CV2)."""

    def __init__(self, view: "UniversalLeaderboardView", page: int,
                 label: str, disabled: bool):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            disabled=disabled,
            custom_id=f"leaderboard_page:{view.guild_id}:{page}:{view.leaderboard_type}",
        )
        self.target_page = page
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view_ref
        if interaction.user.id != view.author_id:
            await interaction.response.send_message(
                replace_emojis("❌ Вы не можете перелистывать чужие страницы!"),
                ephemeral=True
            )
            return
        view.page = self.target_page
        await view._render()
        await interaction.response.edit_message(view=view)


class UniversalLeaderboardView(discord.ui.LayoutView):
    """CV2-view с пагинацией и выпадающим меню для лидерборда."""

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
        """Initialize total pages и отрендерить карточку."""
        self._total_pages = await player_stats_store.get_total_pages(self.guild_id, self.per_page)
        await self._render()

    async def _render(self) -> None:
        """Пересобрать содержимое LayoutView (карточка + select + навигация)."""
        embed = await build_leaderboard_embed(
            self.guild_id, self.page, self.leaderboard_type, self.user_id)

        self.clear_items()
        self.add_item(container_from_embed(embed))

        # Ряд 1: выбор категории (select занимает весь ряд)
        select_row = discord.ui.ActionRow()
        select_row.add_item(TopCategorySelect(self))
        self.add_item(select_row)

        # Ряд 2: навигация
        nav_row = discord.ui.ActionRow()
        nav_row.add_item(_NavButton(
            self, self.page - 1, "◀", disabled=self.page <= 1))
        nav_row.add_item(_NavButton(
            self, self.page + 1, "▶", disabled=self.page >= self._total_pages))
        self.add_item(nav_row)


class LeaderboardPageButton(discord.ui.Button):
    """Кнопка для перехода на страницу лидерборда (legacy embed-view)."""

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

        if interaction.user.id != getattr(view, "author_id", interaction.user.id):
            await interaction.response.send_message(
                replace_emojis("❌ Вы не можете перелистывать чужие страницы!"),
                ephemeral=True
            )
            return

        view.page = self.page
        view.leaderboard_type = self.leaderboard_type
        if hasattr(view, "refresh_buttons"):
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
