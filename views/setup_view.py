"""View для настройки турнира с кнопками выбора круга."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.tournament import RegistrationState, Tournament, TournamentPhase
from storage.json_store import store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class CircleSelectButton(discord.ui.Button):
    """Кнопка для выбора круга при добавлении игрока."""

    def __init__(self, guild_id: int, circle: int, circle_name: str):
        label = circle_name
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=f"circle_select:{guild_id}:{circle}",
        )
        self.guild_id = guild_id
        self.circle = circle

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.", ephemeral=True
            )
            return

        # Check if registration is open
        if tournament.registration == RegistrationState.CLOSED:
            # Only admin can add
            from utils.permissions import is_admin_check
            if not is_admin_check(interaction.user, interaction.guild):
                await interaction.response.send_message(
                    "❌ Регистрация закрыта. Только админ может добавлять игроков.",
                    ephemeral=True
                )
                return

        # Check if circle is full (except circle4)
        if self.circle != 4:
            circle_list = getattr(tournament, f"circle{self.circle}")
            if len(circle_list) >= 4:
                await interaction.response.send_message(
                    f"❌ Круг {self.circle} уже заполнен (максимум 4 игрока).",
                    ephemeral=True
                )
                return

        # Get user's nickname
        user_name = interaction.user.display_name

        # Check if user already in tournament
        if user_name in tournament.all_players:
            await interaction.response.send_message(
                "❌ Вы уже участвуете в турнире.",
                ephemeral=True
            )
            return

        # Add player
        success = tournament.add_player_to_circle(self.circle, user_name)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось добавить игрока.",
                ephemeral=True
            )
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            f"✅ Вы добавлены в {circle_names[self.circle]}!",
            ephemeral=True
        )


circle_names = {
    1: "Капитаны",
    2: "Круг 2",
    3: "Круг 3",
    4: "Круг 4",
}


class AdminAddModal(discord.ui.Modal):
    """Модальное окно для админа добавления игрока."""

    def __init__(self, guild_id: int, circle: int):
        super().__init__(title=f"Добавить в {circle_names[circle]}")
        self.guild_id = guild_id
        self.circle = circle

        self.name_input = discord.ui.TextInput(
            label="Имя игрока",
            placeholder="Введите имя игрока",
            required=True,
            max_length=50,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.", ephemeral=True
            )
            return

        player_name = self.name_input.value.strip()

        # Check if circle is full (except circle4)
        if self.circle != 4:
            circle_list = getattr(tournament, f"circle{self.circle}")
            if len(circle_list) >= 4:
                await interaction.response.send_message(
                    f"❌ Круг {self.circle} уже заполнен (максимум 4 игрока).",
                    ephemeral=True
                )
                return

        # Check if player already in tournament
        if player_name in tournament.all_players:
            await interaction.response.send_message(
                "❌ Этот игрок уже участвует в турнире.",
                ephemeral=True
            )
            return

        # Add player
        success = tournament.add_player_to_circle(self.circle, player_name)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось добавить игрока.",
                ephemeral=True
            )
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            f"✅ Игрок {player_name} добавлен в {circle_names[self.circle]}!",
            ephemeral=True
        )


class AdminAddButton(discord.ui.Button):
    """Кнопка для админа добавления игрока в конкретный круг."""

    def __init__(self, guild_id: int, circle: int, circle_name: str):
        label = f"+ {circle_name}"
        super().__init__(
            style=discord.ButtonStyle.success,
            label=label,
            custom_id=f"admin_add:{guild_id}:{circle}",
        )
        self.guild_id = guild_id
        self.circle = circle

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils.permissions import is_admin_check
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "❌ Только админ может добавлять игроков.",
                ephemeral=True
            )
            return

        # Show modal
        modal = AdminAddModal(self.guild_id, self.circle)
        await interaction.response.send_modal(modal)


class SetupView(discord.ui.View):
    """View с кнопками выбора круга для добавления игроков."""

    def __init__(self, tournament: Tournament):
        super().__init__(timeout=None)
        self.tournament = tournament

        if tournament.registration == RegistrationState.OPEN:
            # Open registration: 4 buttons for players to add themselves
            for circle in range(1, 5):
                button = CircleSelectButton(tournament.guild_id, circle, circle_names[circle])
                self.add_item(button)
        else:
            # Closed registration: 4 buttons for admin to add players
            for circle in range(1, 5):
                button = AdminAddButton(tournament.guild_id, circle, circle_names[circle])
                self.add_item(button)


def build_setup_view(tournament: Tournament) -> SetupView:
    """Создать View для фазы настройки."""
    if tournament.phase != TournamentPhase.SETUP:
        return None
    return SetupView(tournament)
