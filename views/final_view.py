
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
from utils.permissions import is_org_check
from views.room_buttons import RoomButton, AdminRoomsButton

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


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
        if not is_org_check(interaction.user, interaction.guild):
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

        # Admin-панель (победитель/статистика/комнаты) убрана —
        # результаты идут через OCR-скриншот, комнаты — через RoomButton

        # Team name button disabled - no team name changes allowed
        # has_editable_team = any(tournament.is_team_name_editable(i) for i in range(len(tournament.teams)))
        # if has_editable_team:
        #     from views.matches_view import TeamNameButton
        #     self.add_item(TeamNameButton(guild_id, tournament))

        # Add betting buttons (skip for random-auto)
        if tournament.formation_mode != "random-auto":
            from views.bet_views import BetButton, ViewBetsButton
            final_matches = [(final_teams[0], final_teams[1])]
            self.add_item(BetButton(guild_id, channel_id, tournament, final_matches, "final"))
            self.add_item(ViewBetsButton(guild_id, channel_id, tournament, final_matches, "final"))

        # Add room button for final (only if not filled)
        if not tournament.final_room:
            team_a = final_teams[0]
            team_b = final_teams[1]
            team_a_data = tournament.teams[team_a] if team_a < len(tournament.teams) else {}
            team_b_data = tournament.teams[team_b] if team_b < len(tournament.teams) else {}
            captain_a = team_a_data.get("captain", f"П{team_a + 1}")
            captain_b = team_b_data.get("captain", f"П{team_b + 1}")
            name_a = tournament.team_names.get(team_a, captain_a)
            name_b = tournament.team_names.get(team_b, captain_b)

            self.add_item(RoomButton("final", 0, team_a, team_b, name_a, name_b, is_admin=False))

        # Кнопка «Отправить скриншот» для финала (пока матч не завершён)
        from views.ocr_match_view import (
            MatchScreenshotButton,
            _stats_complete,
            match_is_finished,
        )
        if (
            tournament.phase == TournamentPhase.FINAL
            and not match_is_finished(tournament, "final", 0)
            and not _stats_complete(tournament, "final", 0)
        ):
            self.add_item(
                MatchScreenshotButton(
                    guild_id, channel_id, tournament,
                    "final", 0, "📸 Финал",
                )
            )
