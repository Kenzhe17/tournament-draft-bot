"""Views for matchmaking system."""

from __future__ annotations

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
            label="Join Matchmaking",
            style=discord.ButtonStyle.success,
            custom_id="mm_join"
        )
        self.join_button.callback = self.join_callback
        self.add_item(self.join_button)

    async def join_callback(self, interaction: discord.Interaction):
        """Обработка нажатия кнопки Join."""
        user_id = interaction.user.id
        user_name = interaction.user.display_name

        success, message = matchmaking_manager.add_player(self.guild_id, user_id, user_name)

        if success:
            await interaction.response.send_message(f"✅ {message}", ephemeral=True)
            # Обновить embed
            await self.update_embed(interaction)
        else:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)

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
            # Если собрано 8 игроков, создаем закрытый канал
            await self.create_private_channel(interaction, session)
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

        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed)
            else:
                await interaction.response.edit_message(embed=embed)
        except discord.NotFound:
            pass

    async def create_private_channel(self, interaction: discord.Interaction, session):
        """Создать закрытый канал для матча."""
        if session.match.channel_id:
            return  # Канал уже создан

        guild = interaction.guild
        category = guild.get_channel(1519430641085710346)  # ID категории для закрытых каналов

        if not category:
            logger.error("Категория для закрытых каналов не найдена")
            return

        # Создаем закрытый канал
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        # Даем доступ всем игрокам
        for player_id in session.match.players:
            member = guild.get_member(player_id)
            if member:
                overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel = await guild.create_text_channel(
            f"match-{session.match_id[:8]}",
            category=category,
            overwrites=overwrites
        )

        session.match.channel_id = channel.id
        session.start_draft()

        # Отправляем сообщение в закрытый канал
        await self.send_match_start_message(channel, session)

        # Выбираем капитанов
        await self.select_captains(channel, session)

    async def send_match_start_message(self, channel, session):
        """Отправить сообщение о начале матча в закрытый канал."""
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
