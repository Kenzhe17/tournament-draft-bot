"""View для полуфиналов — кнопки победителей и генерации матчей."""

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


class GenerateMatchesButton(discord.ui.Button):
    """Кнопка генерации полуфинальных пар."""

    def __init__(self, guild_id: int):
        super().__init__(
            label="🎲 Generate Matches",
            style=discord.ButtonStyle.primary,
            custom_id=f"generate_matches:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Только администраторы могут генерировать матчи.",
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.TEAMS:
            await interaction.response.send_message(
                "❌ Невозможно сгенерировать матчи.", ephemeral=True
            )
            return

        tournament.generate_semifinals()
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


class SemifinalWinnerButton(discord.ui.Button):
    """Кнопка выбора победителя полуфинала."""

    def __init__(self, guild_id: int, match_index: int, team_index: int):
        super().__init__(
            label=f"Team {team_index + 1} Won",
            style=discord.ButtonStyle.success,
            custom_id=f"semi_win:{guild_id}:{match_index}:{team_index}",
        )
        self.guild_id = guild_id
        self.match_index = match_index
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Только администраторы могут фиксировать результаты.",
                ephemeral=True,
            )
            return

        tournament = store.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SEMIFINALS:
            await interaction.response.send_message(
                "❌ Полуфиналы не активны.", ephemeral=True
            )
            return

        # Проверяем, что team_index — участник этого матча
        match = tournament.semifinal_matches[self.match_index]
        if self.team_index not in match:
            await interaction.response.send_message(
                "❌ Неверная команда для этого матча.", ephemeral=True
            )
            return

        if tournament.semifinal_winners[self.match_index] is not None:
            await interaction.response.send_message(
                "❌ Результат этого матча уже зафиксирован.", ephemeral=True
            )
            return

        both_done = tournament.set_semifinal_winner(
            self.match_index, self.team_index
        )
        store.set(tournament)

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)
        await interaction.response.defer()


class TeamsView(discord.ui.View):
    """View с кнопкой генерации матчей после драфта."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.add_item(GenerateMatchesButton(guild_id))


class SemifinalsView(discord.ui.View):
    """View с кнопками победителей полуфиналов."""

    def __init__(self, guild_id: int, matches: list[tuple[int, int]], winners: list):
        super().__init__(timeout=None)
        for i, (team_a, team_b) in enumerate(matches):
            if winners[i] is not None:
                continue
            self.add_item(SemifinalWinnerButton(guild_id, i, team_a))
            self.add_item(SemifinalWinnerButton(guild_id, i, team_b))
