"""View для настройки турнира с кнопками выбора круга."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from models.tournament import RegistrationState, Tournament, TournamentPhase, TournamentSize
from storage.json_store import store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


async def _delete_ephemeral_later(interaction: discord.Interaction, delay: float = 4.0) -> None:
    """Удалить ephemeral-ответ через указанное время."""
    await asyncio.sleep(delay)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass


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
            asyncio.create_task(_delete_ephemeral_later(interaction))
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
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return
            # Admin can add via modal
            modal = AdminAddModal(self.guild_id, self.circle)
            await interaction.response.send_modal(modal)
            return

        # Check if circle is full (except circle4)
        if self.circle != 4:
            circle_list = getattr(tournament, f"circle{self.circle}")
            limit = tournament.circle_limit(self.circle)
            if len(circle_list) >= limit:
                await interaction.response.send_message(
                    f"❌ Круг {self.circle} уже заполнен (максимум {limit} игрока).",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

        # Get user's nickname
        user_name = interaction.user.display_name

        # Check if user already in tournament - if so, move them to new circle
        was_moved = False
        if user_name in tournament.all_players:
            # Find which circle they're in
            for circle in range(1, 5):
                if user_name in getattr(tournament, f"circle{circle}"):
                    if circle == self.circle:
                        await interaction.response.send_message(
                            "❌ Вы уже находитесь в этом круге.",
                            ephemeral=True
                        )
                        asyncio.create_task(_delete_ephemeral_later(interaction))
                        return
                    # Remove from old circle
                    tournament.remove_player(user_name)
                    was_moved = True
                    break

        # Add player with user_id
        success = tournament.add_player_to_circle(self.circle, user_name, interaction.user.id)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось добавить игрока.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        if was_moved:
            await interaction.response.send_message(
                f"✅ Вы перемещены в {circle_names[self.circle]}!",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
        else:
            await interaction.response.send_message(
                f"✅ Вы добавлены в {circle_names[self.circle]}!",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))


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
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        player_name = self.name_input.value.strip()

        # Check if circle is full (except circle4)
        if self.circle != 4:
            circle_list = getattr(tournament, f"circle{self.circle}")
            limit = tournament.circle_limit(self.circle)
            if len(circle_list) >= limit:
                await interaction.response.send_message(
                    f"❌ Круг {self.circle} уже заполнен (максимум {limit} игрока).",
                    ephemeral=True
                )
                asyncio.create_task(_delete_ephemeral_later(interaction))
                return

        # Check if player already in tournament
        if player_name in tournament.all_players:
            await interaction.response.send_message(
                "❌ Этот игрок уже участвует в турнире.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Get user_id from Discord member
        user_id = 0
        for member in interaction.guild.members:
            if member.display_name == player_name:
                user_id = member.id
                break

        # Add player with user_id
        success = tournament.add_player_to_circle(self.circle, player_name, user_id)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось добавить игрока.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            f"✅ Игрок {player_name} добавлен в {circle_names[self.circle]}!",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))


class ExitButton(discord.ui.Button):
    """Кнопка для выхода из турнира."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти",
            custom_id=f"exit:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(interaction.guild_id)
        if not tournament:
            await interaction.response.send_message(
                "❌ Турнир не найден.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if tournament.phase != TournamentPhase.SETUP:
            await interaction.response.send_message(
                "❌ Турнир не в фазе настройки.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        user_name = interaction.user.display_name

        # Check if user is in tournament
        if user_name not in tournament.all_players:
            await interaction.response.send_message(
                "❌ Вы не участвуете в турнире.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Remove player
        success = tournament.remove_player(user_name)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось удалить игрока.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            "✅ Вы вышли из турнира.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))


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
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Show modal
        modal = AdminAddModal(self.guild_id, self.circle)
        await interaction.response.send_modal(modal)


class SetupView(discord.ui.View):
    """View с кнопками выбора круга для добавления игроков."""

    def __init__(self, tournament: Tournament):
        super().__init__(timeout=None)
        self.tournament = tournament
        self.registration_state = tournament.registration

        # Always show all 4 circles with the same buttons
        # The button logic will handle open vs closed registration
        for circle in range(1, 5):
            button = CircleSelectButton(tournament.guild_id, circle, circle_names[circle])
            self.add_item(button)
        
        # Add exit button
        exit_button = ExitButton(tournament.guild_id)
        self.add_item(exit_button)


def build_setup_view(tournament: Tournament) -> SetupView:
    """Создать View для фазы настройки."""
    if tournament.phase != TournamentPhase.SETUP:
        return None
    return SetupView(tournament)
