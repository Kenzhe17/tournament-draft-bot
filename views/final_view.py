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

        if tournament.final_pending_winner is not None:
            await interaction.response.send_message(
                "❌ Результат финала уже выбран. Ожидается заполнение статистики.", ephemeral=True
            )
            return

        tournament.set_final_winner(self.team_index)
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.send_message(
            f"✅ Победитель выбран. Капитаны команд могут заполнить статистику.",
            ephemeral=True
        )


class FinalView(discord.ui.View):
    """View с кнопками победителей финала."""

    def __init__(self, guild_id: int, final_teams: list[int], tournament):
        super().__init__(timeout=None)

        # Add single winner selection button
        from views.matches_view import SelectWinnerButton
        self.add_item(SelectWinnerButton(guild_id, tournament, "final"))

        # Add betting buttons
        from views.bet_views import BetButton, ViewBetsButton, ToggleBettingButton
        final_matches = [(final_teams[0], final_teams[1])]
        self.add_item(BetButton(guild_id, tournament, final_matches, "final"))
        self.add_item(ViewBetsButton(guild_id, tournament, final_matches, "final"))
        self.add_item(ToggleBettingButton(guild_id, tournament.betting_open))

        # Add admin fill button
        from views.match_stats_view import AdminFillButton
        self.add_item(AdminFillButton(guild_id, tournament))

        # Add captain fill buttons for pending final
        from views.match_stats_view import CaptainFillButton
        if tournament.final_pending_winner is not None:
            match_id = "final_0"
            # Check if team 0 has filled stats
            team0_filled = any(
                tournament.teams[final_teams[0]].get(f"circle{c}") in tournament.temp_match_stats.get(match_id, {})
                for c in range(1, 5)
            )
            if not team0_filled:
                self.add_item(CaptainFillButton(guild_id, tournament, "final", 0, final_teams[0]))
            # Check if team 1 has filled stats
            team1_filled = any(
                tournament.teams[final_teams[1]].get(f"circle{c}") in tournament.temp_match_stats.get(match_id, {})
                for c in range(1, 5)
            )
            if not team1_filled:
                self.add_item(CaptainFillButton(guild_id, tournament, "final", 0, final_teams[1]))
