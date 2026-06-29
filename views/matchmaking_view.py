"""Views for matchmaking system."""

from __future__ import annotations

import logging
import discord
from discord.ui import View, Button
from typing import TYPE_CHECKING

from matchmaking.manager import matchmaking_manager

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class MatchmakingView(View):
    """View для matchmaking в главном канале."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

        self.join_button = Button(
            label="Join",
            style=discord.ButtonStyle.success,
            custom_id="mm_join"
        )
        self.join_button.callback = self.join_callback
        self.add_item(self.join_button)

        self.leave_button = Button(
            label="Leave",
            style=discord.ButtonStyle.danger,
            custom_id="mm_leave"
        )
        self.leave_button.callback = self.leave_callback
        self.add_item(self.leave_button)

    async def join_callback(self, interaction: discord.Interaction):
        """Обработка нажатия кнопки Join."""
        user_id = interaction.user.id
        user_name = interaction.user.display_name

        success, message = matchmaking_manager.add_player(
            self.guild_id, user_id, user_name, interaction.channel_id
        )

        if success:
            await interaction.response.send_message(f"✅ {message}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)

        # Обновляем embed через менеджер для всех
        bot = interaction.client
        await matchmaking_manager.update_main_embed(self.guild_id, bot)

    async def leave_callback(self, interaction: discord.Interaction):
        """Обработка нажатия кнопки Leave."""
        user_id = interaction.user.id

        success = matchmaking_manager.remove_player(self.guild_id, user_id)

        if success:
            await interaction.response.send_message("✅ Вы покинули Matchmaking", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Вы не находитесь в Matchmaking", ephemeral=True)

        # Обновляем embed через менеджер для всех
        bot = interaction.client
        await matchmaking_manager.update_main_embed(self.guild_id, bot)

    async def update_embed(self, interaction: discord.Interaction):
        """Обновить embed с количеством игроков."""
        session = matchmaking_manager.get_session(self.guild_id)
        if not session:
            return

        player_count = session.get_player_count()
        player_names = [session.match.player_names.get(pid, "Unknown") for pid in session.match.players]

        embed = discord.Embed(
            title="🎮 Matchmaking Lobby",
            color=discord.Color.blue()
        )

        if session.is_full():
            embed.description = "🎉 **Match Found!**\n\n8/8 игроков собрано."
            # Если собрано 8 игроков, начинаем драфт в этом же канале
            await self.start_matchmaking_flow(interaction, session)
        else:
            embed.description = f"Поиск игры:\n{player_count}/8 игроков"

        # Список игроков
        players_text = ""
        for i in range(8):
            if i < len(player_names):
                players_text += f"{i + 1}. {player_names[i]}\n"
            else:
                players_text += f"{i + 1}.\n"

        embed.add_field(name="Players:", value=players_text, inline=False)

        # Сохраняем ссылку на сообщение
        if not session.match.main_message_id:
            session.match.main_message_id = interaction.message.id

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed)
            else:
                await interaction.response.edit_message(embed=embed)
        except discord.NotFound:
            pass

    async def start_matchmaking_flow(self, interaction: discord.Interaction, session):
        """Начать драфт в главном канале."""
        session.start_draft()

        # Отправляем сообщение в главном канале
        await self.send_match_start_message(interaction.channel, session)

        # Выбираем капитанов
        await self.select_captains(interaction.channel, session)

    async def send_match_start_message(self, channel, session):
        """Отправить сообщение о начале матча в главном канале."""
        player_names = [session.match.player_names.get(pid, "Unknown") for pid in session.match.players]

        embed = discord.Embed(
            title="🎮 Match Found!",
            description="8 игроков собрано. Начинаем драфт!",
            color=discord.Color.green()
        )

        players_text = "\n".join([f"{i + 1}. {name}" for i, name in enumerate(player_names)])
        embed.add_field(name="Players:", value=players_text, inline=False)

        await channel.send(embed=embed)

    async def select_captains(self, channel, session):
        """Выбрать 2 капитанов из 8 игроков."""
        import random

        players = session.match.players.copy()
        random.shuffle(players)

        captain1_id = players[0]
        captain2_id = players[1]
        captain1_name = session.match.player_names[captain1_id]
        captain2_name = session.match.player_names[captain2_id]

        session.create_teams(captain1_id, captain1_name, captain2_id, captain2_name)

        embed = discord.Embed(
            title="👑 Капитаны выбраны",
            color=discord.Color.gold()
        )
        embed.add_field(name="Team 1 Captain", value=captain1_name, inline=True)
        embed.add_field(name="Team 2 Captain", value=captain2_name, inline=True)

        await channel.send(embed=embed)

        # Подготовить драфт
        await self.prepare_draft(channel, session)

    async def prepare_draft(self, channel, session):
        """Подготовить драфт - распределить игроков по командам."""
        import random

        # Получаем всех игроков кроме капитанов
        captain_ids = {session.match.teams[0].captain_id, session.match.teams[1].captain_id}
        available_players = [pid for pid in session.match.players if pid not in captain_ids]

        # Распределяем игроков по кругам (круг 2, 3, 4 по 3 игрока каждый)
        session.match.draft_data = {
            "available": available_players,
            "circle": 2,
            "pick_index": 0,
            "picks": {
                "team0": {"circle2": None, "circle3": None, "circle4": None},
                "team1": {"circle2": None, "circle3": None, "circle4": None},
            }
        }

        # Добавляем капитанов в команды
        session.match.teams[0].players = [session.match.teams[0].captain_id]
        session.match.teams[1].players = [session.match.teams[1].captain_id]

        embed = discord.Embed(
            title="🎲 Драфт",
            description="Капитаны выбирают игроков по очереди.",
            color=discord.Color.blue()
        )

        await channel.send(embed=embed)

        # Начинаем драфт - упрощенная версия: случайное распределение
        await self.auto_draft(channel, session)

    async def auto_draft(self, channel, session):
        """Автоматически распределить игроков по командам (упрощенный драфт)."""
        import random

        available = session.match.draft_data["available"].copy()
        random.shuffle(available)

        # Распределяем по 3 игрока каждой команде
        team0_players = available[:3]
        team1_players = available[3:6]

        session.match.teams[0].players.extend(team0_players)
        session.match.teams[1].players.extend(team1_players)

        # Показываем результаты драфта
        team0_names = [session.match.player_names[pid] for pid in session.match.teams[0].players]
        team1_names = [session.match.player_names[pid] for pid in session.match.teams[1].players]

        embed = discord.Embed(
            title="✅ Драфт завершен",
            color=discord.Color.green()
        )

        embed.add_field(
            name=f"{session.match.teams[0].name}",
            value="\n".join(team0_names),
            inline=True
        )
        embed.add_field(
            name=f"{session.match.teams[1].name}",
            value="\n".join(team1_names),
            inline=True
        )

        await channel.send(embed=embed)

        # Переходим к фазе настройки команд
        session.start_team_setup()

        # Показываем TeamSetupView
        from views.team_setup_view import TeamSetupView
        view = TeamSetupView(session.guild_id, session)

        setup_embed = discord.Embed(
            title="⚙️ Настройка команд",
            description="Капитаны могут изменить название команды и нажать Ready когда будут готовы.",
            color=discord.Color.orange()
        )

        await channel.send(embed=setup_embed, view=view)

        # Добавляем кнопки для ставок
        from views.bet_views import BetButton, ViewBetsButton, ToggleBettingButton
        matches = [(0, 1)]  # Одна игра между командой 0 и командой 1

        betting_view = discord.ui.View(timeout=None)
        betting_view.add_item(BetButton(session.guild_id, session.match, matches, "matchmaking"))
        betting_view.add_item(ViewBetsButton(session.guild_id, session.match, matches, "matchmaking"))
        betting_view.add_item(ToggleBettingButton(session.guild_id, session.match.betting_open))

        betting_embed = discord.Embed(
            title="💰 Ставки",
            description="Делайте ставки на матч! Ставки закроются когда обе команды нажмут Ready.",
            color=discord.Color.gold()
        )

        await channel.send(embed=betting_embed, view=betting_view)

        # Сохраняем ссылку на канал для дальнейшего использования
        session.match.channel_id = channel.id
