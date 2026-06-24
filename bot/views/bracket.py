"""Кнопки для плей-офф: полуфиналы и финал."""

from __future__ import annotations

import logging

import discord

from bot.config import VIEW_FINAL_PREFIX, VIEW_GENERATE_MATCHES, VIEW_SEMIFINAL_PREFIX
from bot.draft_engine import generate_semifinal_pairs
from bot.models import BracketState, TournamentPhase
from bot.storage import storage

logger = logging.getLogger(__name__)


class GenerateMatchesButton(discord.ui.Button):
    """Кнопка генерации полуфинальных пар."""

    def __init__(self, guild_id: int) -> None:
        super().__init__(
            label="🎲 Generate Matches",
            style=discord.ButtonStyle.primary,
            custom_id=f"{VIEW_GENERATE_MATCHES}:{guild_id}",
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = storage.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.TEAMS:
            await interaction.response.send_message("❌ Неверная фаза турнира.", ephemeral=True)
            return

        pairs = generate_semifinal_pairs()
        tournament.bracket = BracketState(semifinal_pairs=pairs)
        tournament.phase = TournamentPhase.SEMIFINALS
        storage.save(tournament)

        from bot.message_manager import update_tournament_message

        await interaction.response.defer()
        if interaction.guild:
            await update_tournament_message(interaction.client, interaction.guild, tournament)


class GenerateMatchesView(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(GenerateMatchesButton(guild_id))


class SemifinalWinButton(discord.ui.Button):
    """Кнопка победы команды в полуфинале."""

    def __init__(self, guild_id: int, match_num: int, team_num: int) -> None:
        super().__init__(
            label=f"Team {team_num} Won",
            style=discord.ButtonStyle.success,
            custom_id=f"{VIEW_SEMIFINAL_PREFIX}{guild_id}:{match_num}:{team_num}",
        )
        self.guild_id = guild_id
        self.match_num = match_num
        self.team_num = team_num

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = storage.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.SEMIFINALS or not tournament.bracket:
            await interaction.response.send_message("❌ Полуфинал не активен.", ephemeral=True)
            return

        bracket = tournament.bracket
        if self.match_num < 1 or self.match_num > len(bracket.semifinal_pairs):
            await interaction.response.send_message("❌ Матч не найден.", ephemeral=True)
            return

        pair = bracket.semifinal_pairs[self.match_num - 1]
        if self.team_num not in pair:
            await interaction.response.send_message("❌ Эта команда не участвует в матче.", ephemeral=True)
            return

        if self.match_num in bracket.semifinal_winners:
            await interaction.response.send_message("❌ Результат матча уже определён.", ephemeral=True)
            return

        bracket.semifinal_winners[self.match_num] = self.team_num

        # Оба полуфинала завершены — создаём финал
        if len(bracket.semifinal_winners) == len(bracket.semifinal_pairs):
            winners = [bracket.semifinal_winners[i] for i in range(1, len(bracket.semifinal_pairs) + 1)]
            bracket.final_pair = (winners[0], winners[1])
            tournament.phase = TournamentPhase.FINAL

        storage.save(tournament)

        from bot.message_manager import update_tournament_message

        await interaction.response.defer()
        if interaction.guild:
            await update_tournament_message(interaction.client, interaction.guild, tournament)


class BracketView(discord.ui.View):
    """Кнопки победы для обоих полуфиналов."""

    def __init__(self, guild_id: int, bracket: BracketState) -> None:
        super().__init__(timeout=None)
        for match_num, (t1, t2) in enumerate(bracket.semifinal_pairs, start=1):
            if match_num not in bracket.semifinal_winners:
                self.add_item(SemifinalWinButton(guild_id, match_num, t1))
                self.add_item(SemifinalWinButton(guild_id, match_num, t2))


class FinalWinButton(discord.ui.Button):
    """Кнопка победы в финале."""

    def __init__(self, guild_id: int, team_num: int) -> None:
        super().__init__(
            label=f"Team {team_num} Won",
            style=discord.ButtonStyle.danger,
            custom_id=f"{VIEW_FINAL_PREFIX}{guild_id}:{team_num}",
        )
        self.guild_id = guild_id
        self.team_num = team_num

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = storage.get(self.guild_id)
        if not tournament or tournament.phase != TournamentPhase.FINAL or not tournament.bracket:
            await interaction.response.send_message("❌ Финал не активен.", ephemeral=True)
            return

        bracket = tournament.bracket
        if not bracket.final_pair or self.team_num not in bracket.final_pair:
            await interaction.response.send_message("❌ Эта команда не в финале.", ephemeral=True)
            return

        if bracket.winner_team is not None:
            await interaction.response.send_message("❌ Победитель уже определён.", ephemeral=True)
            return

        bracket.winner_team = self.team_num
        tournament.phase = TournamentPhase.COMPLETE
        storage.save(tournament)

        from bot.message_manager import update_tournament_message

        await interaction.response.defer()
        if interaction.guild:
            await update_tournament_message(interaction.client, interaction.guild, tournament)


class FinalView(discord.ui.View):
    def __init__(self, guild_id: int, bracket: BracketState) -> None:
        super().__init__(timeout=None)
        if bracket.final_pair and bracket.winner_team is None:
            t1, t2 = bracket.final_pair
            self.add_item(FinalWinButton(guild_id, t1))
            self.add_item(FinalWinButton(guild_id, t2))
