
"""View для финала — кнопки победителя."""

from __future__ import annotations

from config import replace_emojis

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import TournamentPhase
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from utils.embeds import build_embed_for_phase
from utils.permissions import can_manage_channel_tournament

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


def _team_stats_filled(tournament, match_id: str, team_index: int) -> bool:
    """Все игроки команды внесли K/D в temp_match_stats."""
    from views.matches_view import team_roster_names

    stats = tournament.temp_match_stats.get(match_id, {})
    roster = team_roster_names(tournament, team_index)
    return bool(roster) and all(p in stats for p in roster)


class FinalWinnerButton(discord.ui.Button):
    """Кнопка выбора победителя финала."""

    def __init__(self, guild_id: int, channel_id: int, team_index: int, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.danger,
            custom_id=f"final_win:{guild_id}:{channel_id}:{team_index}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_manage_channel_tournament(interaction.user, interaction.guild, self.channel_id):
            await interaction.response.send_message(
                replace_emojis("❌ Только организаторы (роль 'org') могут фиксировать результаты."),
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament or tournament.phase != TournamentPhase.FINAL:
            await interaction.response.send_message(
                replace_emojis("❌ Финал не активен."),
                ephemeral=True,
            )
            return

        if self.team_index not in tournament.final_teams:
            await interaction.response.send_message(
                replace_emojis("❌ Неверная команда."),
                ephemeral=True,
            )
            return

        if tournament.final_pending_winner is not None:
            await interaction.response.send_message(
                replace_emojis("❌ Результат финала уже выбран. Ожидается заполнение статистики."),
                ephemeral=True,
            )
            return

        tournament.set_final_winner(self.team_index)
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.send_message(
            replace_emojis("✅ Победитель выбран. Капитаны команд могут заполнить статистику."),
            ephemeral=True,
        )


class FinalView(discord.ui.View):
    """View с кнопками победителей финала."""

    def __init__(self, guild_id: int, channel_id: int, final_teams: list[int], tournament):
        super().__init__(timeout=None)

        # Панель организатора: победитель / статистика (r!z3 CLASSIC)
        from views.matches_view import AdminPanelSelect
        self.add_item(AdminPanelSelect(guild_id, channel_id, tournament, "final"))

        # Team name button disabled - no team name changes allowed
        # has_editable_team = any(tournament.is_team_name_editable(i) for i in range(len(tournament.teams)))
        # if has_editable_team:
        #     from views.matches_view import TeamNameButton
        #     self.add_item(TeamNameButton(guild_id, tournament))

        # Ставки на финал
        from views.bet_views import BetButton, ViewBetsButton
        final_matches = [(final_teams[0], final_teams[1])]
        self.add_item(BetButton(guild_id, channel_id, tournament, final_matches, "final"))
        self.add_item(ViewBetsButton(guild_id, channel_id, tournament, final_matches, "final"))

        # Кнопки капитанов «Заполнить K/D» — пока команда не внесла статистику
        from views.match_stats_view import CaptainFillButton
        if tournament.phase == TournamentPhase.FINAL:
            match_id = f"{tournament.id}_final_0"
            if not _team_stats_filled(tournament, match_id, final_teams[0]):
                self.add_item(CaptainFillButton(
                    guild_id, channel_id, tournament, "final", 0, final_teams[0]))
            if not _team_stats_filled(tournament, match_id, final_teams[1]):
                self.add_item(CaptainFillButton(
                    guild_id, channel_id, tournament, "final", 0, final_teams[1]))
