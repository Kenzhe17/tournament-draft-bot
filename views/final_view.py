"""View для финала — кнопки победителя."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from utils.embeds import build_embed_for_phase
from utils.permissions import is_admin_check

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
        if not is_admin_check(interaction.user, interaction.guild):
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

        # Update player statistics with ELO
        winning_team = tournament.teams[self.team_index]
        losing_team_index = tournament.final_teams[1] if tournament.final_teams[0] == self.team_index else tournament.final_teams[0]
        losing_team = tournament.teams[losing_team_index]

        # Update winning team stats (+25 ELO)
        for circle in range(1, 5):
            player = winning_team.get(f"circle{circle}")
            if player:
                await player_stats_store.update_player(tournament.guild_id, player, result="win", count_game=False)

        # Update losing team stats (+10 ELO for finalist)
        for circle in range(1, 5):
            player = losing_team.get(f"circle{circle}")
            if player:
                await player_stats_store.update_player(tournament.guild_id, player, result="final", count_game=False)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


class FinalView(discord.ui.View):
    """View с кнопками победителей финала."""

    def __init__(self, guild_id: int, final_teams: list[int], tournament):
        super().__init__(timeout=None)
        for team_idx in final_teams:
            # Get team name or default to captain name
            team_data = tournament.teams[team_idx] if team_idx < len(tournament.teams) else {}
            captain = team_data.get("captain", f"П{team_idx + 1}")
            team_name = tournament.team_names.get(team_idx, captain)

            self.add_item(
                FinalWinnerButton(
                    guild_id,
                    team_idx,
                    label=f"{team_name} победил",
                )
            )
