"""View для настройки турнира с кнопками плюс."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import RegistrationState, Tournament, TournamentPhase
from storage.json_store import store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class PlusButton(discord.ui.Button):
    """Кнопка плюс для добавления игрока в конкретный слот."""

    def __init__(self, guild_id: int, circle: int, slot: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="+",
            custom_id=f"plus:{guild_id}:{circle}:{slot}",
        )
        self.guild_id = guild_id
        self.circle = circle
        self.slot = slot

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

        # Get user's nickname
        user_name = interaction.user.display_name

        # Try to add to the specific circle
        circle_list = getattr(tournament, f"circle{self.circle}")
        
        # Check if slot is already filled
        if self.slot < len(circle_list):
            await interaction.response.send_message(
                "❌ Этот слот уже занят.", ephemeral=True
            )
            return

        # Check if user already in tournament
        if user_name in tournament.all_players:
            await interaction.response.send_message(
                "❌ Вы уже участвуете в турнире.", ephemeral=True
            )
            return

        # Add player
        success = tournament.add_player_to_circle(self.circle, user_name)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось добавить игрока.", ephemeral=True
            )
            return

        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        await interaction.response.send_message(
            f"✅ Вы добавлены в круг {self.circle}!", ephemeral=True
        )


class SetupView(discord.ui.View):
    """View с кнопками плюс для каждого слота каждого круга."""

    def __init__(self, tournament: Tournament):
        super().__init__(timeout=None)
        self.tournament = tournament
        
        # Add plus buttons for each circle and slot
        # Circle1, circle2, circle3 - max 4 players each
        # Circle4 - unlimited, always show one button
        for circle in range(1, 5):
            circle_list = getattr(tournament, f"circle{circle}")
            
            if circle == 4:
                # Circle4 - always show one button for unlimited players
                button = PlusButton(tournament.guild_id, circle, len(circle_list))
                self.add_item(button)
            else:
                # Circle1, circle2, circle3 - max 4 players
                for slot in range(4):
                    # Only add button if slot is empty
                    if slot >= len(circle_list):
                        button = PlusButton(tournament.guild_id, circle, slot)
                        self.add_item(button)


def build_setup_view(tournament: Tournament) -> SetupView:
    """Создать View для фазы настройки."""
    if tournament.phase != TournamentPhase.SETUP:
        return None
    return SetupView(tournament)
