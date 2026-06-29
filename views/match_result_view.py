"""View for match result selection."""

from __future__ import annotations

import discord
from discord.ui import View, Button
from typing import TYPE_CHECKING

from matchmaking.manager import matchmaking_manager
from storage.player_stats_store import player_stats_store

if TYPE_CHECKING:
    from bot import TournamentBot


class MatchResultView(View):
    """View для выбора победителя матча."""

    def __init__(self, guild_id: int, session):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.session = session

        # Кнопки для каждой команды
        for team in session.match.teams:
            button = Button(
                label=f"🏆 {team.name}",
                style=discord.ButtonStyle.success,
                custom_id=f"mm_win_{team.team_id}"
            )
            button.callback = self._create_win_callback(team.team_id)
            self.add_item(button)

    def _create_win_callback(self, team_id: int):
        """Создать callback для кнопки выбора победителя."""
        async def callback(interaction: discord.Interaction):
            # Проверяем, является ли пользователь капитаном этой команды
            team = self.session.match.get_captain_team(interaction.user.id)
            if not team:
                await interaction.response.send_message(
                    "❌ Только капитан может выбирать победителя",
                    ephemeral=True
                )
                return

            # Завершаем матч
            await self.complete_match(interaction, team_id)
        return callback

    async def complete_match(self, interaction: discord.Interaction, winner_team_id: int):
        """Завершить матч и обновить статистику."""
        self.session.complete_match(winner_team_id)

        winner_team = self.session.match.teams[winner_team_id]
        loser_team = self.session.match.teams[1 - winner_team_id]

        # Обновляем ELO
        await self.update_elo(winner_team, loser_team)

        embed = discord.Embed(
            title="🎉 Матч завершен!",
            color=discord.Color.gold()
        )

        embed.add_field(
            name=f"🏆 Победитель: {winner_team.name}",
            value="+10 ELO",
            inline=False
        )
        embed.add_field(
            name=f"💔 Проигравший: {loser_team.name}",
            value="-10 ELO",
            inline=False
        )

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=None)
            else:
                await interaction.response.edit_message(embed=embed, view=None)
        except discord.NotFound:
            pass

        # Сбрасываем сессию для нового матча
        matchmaking_manager.reset_session(self.guild_id)

        # Отправляем финальное сообщение
        await interaction.followup.send(
            "✅ Статистика обновлена. Матч завершен. Начинается новый матч!",
            ephemeral=True
        )

        # Обновляем embed в главном канале
        bot = interaction.client
        await matchmaking_manager.update_main_embed(self.guild_id, bot)

    async def update_elo(self, winner_team, loser_team):
        """Обновить ELO для всех игроков."""
        # Победители +10 ELO
        for player_id in winner_team.players:
            stats = await player_stats_store.get_stats(self.guild_id, player_id)
            if stats:
                stats.elo += 10
                stats.wins += 1
                stats.games += 1
                stats.total_elo_change += 10
                stats.last_elo_change = 10
                await player_stats_store.save_stats(stats)

        # Проигравшие -10 ELO
        for player_id in loser_team.players:
            stats = await player_stats_store.get_stats(self.guild_id, player_id)
            if stats:
                stats.elo -= 10
                stats.games += 1
                stats.total_elo_change -= 10
                stats.last_elo_change = -10
                await player_stats_store.save_stats(stats)

        # Выплаты ставок
        await self.resolve_bets(winner_team.name)

    async def resolve_bets(self, winning_team_name: str):
        """Разрешить ставки и выплатить победителям."""
        from storage.bet_store import bet_store
        from storage.user_balance_store import user_balance_store

        match_id = f"matchmaking_{self.session.match.match_id}"
        payouts = await bet_store.resolve_match_bets(self.guild_id, match_id, winning_team_name)

        # Выплачиваем победителям
        for user_id, amount in payouts.items():
            await user_balance_store.add_balance(self.guild_id, user_id, amount)
