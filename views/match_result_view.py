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

            # Устанавливаем ожидание подтверждения
            self.session.match.pending_winner_team_id = team_id
            self.session.match.pending_winner_captain_id = interaction.user.id

            # Показываем view для подтверждения
            await self.show_confirmation_view(interaction, team_id)
        return callback

    async def show_confirmation_view(self, interaction: discord.Interaction, team_id: int):
        """Показать view для подтверждения победы другим капитаном."""
        team = self.session.match.teams[team_id]
        other_team = self.session.match.teams[1 - team_id]

        embed = discord.Embed(
            title="⚠️ Подтверждение победы",
            description=f"Капитан {team.name} выбрал свою команду как победителя.\n\n"
                       f"Капитан {other_team.name} должен подтвердить или отменить.",
            color=discord.Color.orange()
        )

        view = ConfirmationView(self.guild_id, self.session, team_id)

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
        except discord.NotFound:
            pass

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

        # Показываем результаты матча
        await self.show_match_results(interaction)

        # Сбрасываем сессию для нового матча
        matchmaking_manager.reset_session(self.guild_id)

        # Создаем новый matchmaking embed
        await self.create_new_matchmaking_embed(interaction)

    async def show_match_results(self, interaction: discord.Interaction):
        """Показать результаты матча."""
        winner_team = self.session.match.teams[self.session.match.winner_team_id]
        loser_team = self.session.match.teams[1 - self.session.match.winner_team_id]

        results_embed = discord.Embed(
            title="🏆 Результаты матча",
            color=discord.Color.gold()
        )

        results_embed.add_field(
            name=f"🥇 Победитель: {winner_team.name}",
            value="\n".join([self.session.match.player_names.get(pid, "Unknown") for pid in winner_team.players]),
            inline=False
        )
        results_embed.add_field(
            name=f"🥈 Проигравший: {loser_team.name}",
            value="\n".join([self.session.match.player_names.get(pid, "Unknown") for pid in loser_team.players]),
            inline=False
        )

        await interaction.followup.send(embed=results_embed)

    async def create_new_matchmaking_embed(self, interaction: discord.Interaction):
        """Создать новый matchmaking embed для следующего матча."""
        from views.matchmaking_view import MatchmakingView

        embed = discord.Embed(
            title="🎮 Matchmaking Lobby",
            description="Поиск игры:\n0/8 игроков",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Players:",
            value="1.\n2.\n3.\n4.\n5.\n6.\n7.\n8.",
            inline=False
        )

        view = MatchmakingView(self.guild_id)

        await interaction.followup.send(embed=embed, view=view)

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


class ConfirmationView(discord.ui.View):
    """View для подтверждения победы другим капитаном."""

    def __init__(self, guild_id: int, session, pending_winner_team_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.session = session
        self.pending_winner_team_id = pending_winner_team_id

        confirm_button = Button(
            label="✅ Подтвердить",
            style=discord.ButtonStyle.success,
            custom_id="mm_confirm"
        )
        confirm_button.callback = self.confirm_callback
        self.add_item(confirm_button)

        cancel_button = Button(
            label="❌ Отменить",
            style=discord.ButtonStyle.danger,
            custom_id="mm_cancel"
        )
        cancel_button.callback = self.cancel_callback
        self.add_item(cancel_button)

    async def confirm_callback(self, interaction: discord.Interaction):
        """Подтвердить победу."""
        # Проверяем, является ли пользователь капитаном другой команды
        team = self.session.match.get_captain_team(interaction.user.id)
        if not team or team.team_id == self.pending_winner_team_id:
            await interaction.response.send_message(
                "❌ Только капитан проигравшей команды может подтвердить",
                ephemeral=True
            )
            return

        # Завершаем матч
        await self.complete_match(interaction)

    async def cancel_callback(self, interaction: discord.Interaction):
        """Отменить выбор победителя."""
        # Проверяем, является ли пользователь капитаном другой команды
        team = self.session.match.get_captain_team(interaction.user.id)
        if not team or team.team_id == self.pending_winner_team_id:
            await interaction.response.send_message(
                "❌ Только капитан проигравшей команды может отменить",
                ephemeral=True
            )
            return

        # Сбрасываем ожидание подтверждения
        self.session.match.pending_winner_team_id = None
        self.session.match.pending_winner_captain_id = None

        # Показываем снова view выбора победителя
        view = MatchResultView(self.guild_id, self.session)

        embed = discord.Embed(
            title="⚔️ Завершение матча",
            description="Капитан победившей команды должен выбрать свою команду.",
            color=discord.Color.red()
        )

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
        except discord.NotFound:
            pass

    async def complete_match(self, interaction: discord.Interaction):
        """Завершить матч и обновить статистику."""
        self.session.complete_match(self.pending_winner_team_id)

        winner_team = self.session.match.teams[self.pending_winner_team_id]
        loser_team = self.session.match.teams[1 - self.pending_winner_team_id]

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

        # Показываем результаты матча
        await self.show_match_results(interaction)

        # Сбрасываем сессию для нового матча
        matchmaking_manager.reset_session(self.guild_id)

        # Создаем новый matchmaking embed
        await self.create_new_matchmaking_embed(interaction)

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

    async def show_match_results(self, interaction: discord.Interaction):
        """Показать результаты матча."""
        winner_team = self.session.match.teams[self.pending_winner_team_id]
        loser_team = self.session.match.teams[1 - self.pending_winner_team_id]

        results_embed = discord.Embed(
            title="🏆 Результаты матча",
            color=discord.Color.gold()
        )

        results_embed.add_field(
            name=f"🥇 Победитель: {winner_team.name}",
            value="\n".join([self.session.match.player_names.get(pid, "Unknown") for pid in winner_team.players]),
            inline=False
        )
        results_embed.add_field(
            name=f"🥈 Проигравший: {loser_team.name}",
            value="\n".join([self.session.match.player_names.get(pid, "Unknown") for pid in loser_team.players]),
            inline=False
        )

        await interaction.followup.send(embed=results_embed)

    async def create_new_matchmaking_embed(self, interaction: discord.Interaction):
        """Создать новый matchmaking embed для следующего матча."""
        from views.matchmaking_view import MatchmakingView

        embed = discord.Embed(
            title="🎮 Matchmaking Lobby",
            description="Поиск игры:\n0/8 игроков",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Players:",
            value="1.\n2.\n3.\n4.\n5.\n6.\n7.\n8.",
            inline=False
        )

        view = MatchmakingView(self.guild_id)

        await interaction.followup.send(embed=embed, view=view)
