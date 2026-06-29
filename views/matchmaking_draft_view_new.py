"""View для драфта в matchmaking (новая система)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ui import View, Select

from models.matchmaking import Matchmaking, MatchmakingPhase
from storage.matchmaking_store import matchmaking_store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class MatchmakingPlayerSelect(Select):
    """Select Menu с доступными игроками для драфта."""

    def __init__(self, guild_id: int, available_players: list[str]):
        self.guild_id = guild_id

        # Создаем опции с именами игроков
        options = [
            discord.SelectOption(label=name, value=name) for name in available_players[:25]
        ]

        super().__init__(
            placeholder="Выберите игрока...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"mm_draft_select:{guild_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработка выбора игрока."""
        matchmaking = matchmaking_store.get(self.guild_id)
        if not matchmaking:
            await interaction.response.send_message("❌ Matchmaking не найден", ephemeral=True)
            return

        if matchmaking.phase != MatchmakingPhase.DRAFT:
            await interaction.response.send_message("❌ Драфт не активен", ephemeral=True)
            return

        # Проверяем, чей сейчас ход
        current_picker = matchmaking.draft_picker
        captain_name = matchmaking.team1_captain if current_picker == 0 else matchmaking.team2_captain

        # В тестовом режиме разрешаем выбирать любому
        # В обычном режиме только капитан может выбирать
        if interaction.user.display_name != captain_name:
            await interaction.response.send_message(
                f"❌ Сейчас выбирает капитан {captain_name}",
                ephemeral=True
            )
            return

        # Получаем выбранного игрока
        selected_player = self.values[0]

        # Проверяем, доступен ли игрок
        if selected_player not in matchmaking.available_players:
            await interaction.response.send_message("❌ Этот игрок уже выбран", ephemeral=True)
            return

        # Добавляем игрока в команду
        if current_picker == 0:
            matchmaking.team1_players.append(selected_player)
        else:
            matchmaking.team2_players.append(selected_player)

        # Удаляем из доступных
        matchmaking.available_players.remove(selected_player)

        # Переходим к следующему выбору (snake draft: 0, 1, 1, 0, 1, 0)
        matchmaking.draft_picker = 1 - matchmaking.draft_picker

        # Проверяем, завершен ли драфт (когда все игроки распределены)
        if not matchmaking.available_players:
            # Драфт завершен
            matchmaking.phase = MatchmakingPhase.TEAMS
            matchmaking_store.set(matchmaking)

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_matchmaking_message(interaction.guild, matchmaking)
            await interaction.response.defer()
            return

        matchmaking_store.set(matchmaking)

        # Обновляем embed
        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_matchmaking_message(interaction.guild, matchmaking)
        await interaction.response.defer()


class MatchmakingDraftView(View):
    """View для драфта в matchmaking."""

    def __init__(self, guild_id: int, matchmaking: Matchmaking):
        super().__init__(timeout=None)
        self.guild_id = guild_id

        if matchmaking.available_players:
            self.add_item(MatchmakingPlayerSelect(guild_id, matchmaking.available_players))


def build_matchmaking_draft_view(matchmaking: Matchmaking) -> MatchmakingDraftView | None:
    """Создать View для драфта."""
    if matchmaking.phase != MatchmakingPhase.DRAFT:
        return None
    if not matchmaking.available_players:
        return None
    return MatchmakingDraftView(matchmaking.guild_id, matchmaking)
