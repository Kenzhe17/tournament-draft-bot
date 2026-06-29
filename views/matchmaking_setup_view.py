"""View для настройки matchmaking с кнопками."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord.ui import Button, View

from models.matchmaking import Matchmaking, MatchmakingPhase
from storage.matchmaking_store import matchmaking_store

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


class JoinButton(Button):
    """Кнопка для входа в matchmaking."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Join",
            custom_id=f"mm_join:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        matchmaking = matchmaking_store.get(self.guild_id)
        if not matchmaking:
            await interaction.response.send_message(
                "❌ Matchmaking не найден.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if matchmaking.phase != MatchmakingPhase.SETUP:
            await interaction.response.send_message(
                "❌ Matchmaking не в фазе регистрации.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        user_name = interaction.user.display_name

        # Check if user already in matchmaking
        if matchmaking.is_player_in_matchmaking(user_name):
            await interaction.response.send_message(
                "❌ Вы уже находитесь в Matchmaking.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Check if full
        if matchmaking.is_full:
            await interaction.response.send_message(
                "❌ Matchmaking заполнен (8 игроков).",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Add player
        success = matchmaking.add_player(user_name, interaction.user.id)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось добавить игрока.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        matchmaking_store.set(matchmaking)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_matchmaking_message(interaction.guild, matchmaking)

        await interaction.response.send_message(
            f"✅ Вы добавлены в Matchmaking ({len(matchmaking.players)}/8)!",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))


class LeaveButton(Button):
    """Кнопка для выхода из matchmaking."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Leave",
            custom_id=f"mm_leave:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        matchmaking = matchmaking_store.get(self.guild_id)
        if not matchmaking:
            await interaction.response.send_message(
                "❌ Matchmaking не найден.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if matchmaking.phase != MatchmakingPhase.SETUP:
            await interaction.response.send_message(
                "❌ Matchmaking не в фазе регистрации.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        user_name = interaction.user.display_name

        # Check if user is in matchmaking
        if not matchmaking.is_player_in_matchmaking(user_name):
            await interaction.response.send_message(
                "❌ Вы не участвуете в Matchmaking.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Remove player
        success = matchmaking.remove_player(user_name)
        if not success:
            await interaction.response.send_message(
                "❌ Не удалось удалить игрока.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # If empty, delete matchmaking
        if len(matchmaking.players) == 0:
            matchmaking_store.delete(self.guild_id)
        else:
            matchmaking_store.set(matchmaking)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_matchmaking_message(interaction.guild, matchmaking)

        await interaction.response.send_message(
            "✅ Вы вышли из Matchmaking.",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))


class ReadyButton(Button):
    """Кнопка для готовности."""

    def __init__(self, guild_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Ready",
            custom_id=f"mm_ready:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        matchmaking = matchmaking_store.get(self.guild_id)
        if not matchmaking:
            await interaction.response.send_message(
                "❌ Matchmaking не найден.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        if matchmaking.phase != MatchmakingPhase.SETUP:
            await interaction.response.send_message(
                "❌ Matchmaking не в фазе регистрации.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        user_name = interaction.user.display_name

        # Check if user is in matchmaking
        if not matchmaking.is_player_in_matchmaking(user_name):
            await interaction.response.send_message(
                "❌ Вы не участвуете в Matchmaking.",
                ephemeral=True
            )
            asyncio.create_task(_delete_ephemeral_later(interaction))
            return

        # Ready logic - for now just acknowledge
        await interaction.response.send_message(
            "✅ Вы готовы!",
            ephemeral=True
        )
        asyncio.create_task(_delete_ephemeral_later(interaction))

        # Check if all 8 players are ready (full lobby)
        if matchmaking.is_full:
            # Distribute by ELO and start draft
            await matchmaking.distribute_by_elo(self.guild_id)
            matchmaking.phase = MatchmakingPhase.DRAFT
            matchmaking_store.set(matchmaking)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_matchmaking_message(interaction.guild, matchmaking)


class MatchmakingSetupView(View):
    """View с кнопками для фазы регистрации matchmaking."""

    def __init__(self, matchmaking: Matchmaking):
        super().__init__(timeout=None)
        self.matchmaking = matchmaking

        # Add join button
        join_button = JoinButton(matchmaking.guild_id)
        self.add_item(join_button)

        # Add leave button
        leave_button = LeaveButton(matchmaking.guild_id)
        self.add_item(leave_button)

        # Add ready button
        ready_button = ReadyButton(matchmaking.guild_id)
        self.add_item(ready_button)


def build_matchmaking_setup_view(matchmaking: Matchmaking) -> MatchmakingSetupView:
    """Создать View для фазы регистрации."""
    if matchmaking.phase != MatchmakingPhase.SETUP:
        return None
    return MatchmakingSetupView(matchmaking)
