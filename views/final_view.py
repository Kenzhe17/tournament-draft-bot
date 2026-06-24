"""View для финала — кнопки победителя."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase
from storage.json_store import store
from utils.embeds import build_embed_for_phase

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class FinalWinnerButton(discord.ui.Button):
    """Кнопка выбора победителя финала."""

    def __init__(self, guild_id: int, team_index: int, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.danger,
            custom_id=f"final_win:{guild_id}:{team_index}",
        )
        self.guild_id = guild_id
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Только администраторы могут фиксировать результаты.",
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.FINAL:
            await interaction.response.send_message(
                "❌ Финал не активен.", ephemeral=True
            )
            return

        if self.team_index not in tournament.final_teams:
            await interaction.response.send_message(
                "❌ Неверная команда.", ephemeral=True
            )
            return

        tournament.set_final_winner(self.team_index)
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


class FinalView(discord.ui.View):
    """View с кнопками победителей финала."""

    def __init__(self, guild_id: int, final_teams: list[int]):
        super().__init__(timeout=None)
        for team_idx in final_teams:
            self.add_item(
                FinalWinnerButton(
                    guild_id,
                    team_idx,
                    label=f"П{team_idx + 1} победил",
                )
            )
