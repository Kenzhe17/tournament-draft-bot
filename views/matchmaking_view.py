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

        self.ready_button = Button(
            label="Ready",
            style=discord.ButtonStyle.primary,
            custom_id="mm_ready"
        )
        self.ready_button.callback = self.ready_callback
        self.add_item(self.ready_button)

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

        # Сначала проверяем, находится ли игрок в сессии
        session = matchmaking_manager.get_session(self.guild_id)
        if session and session.is_player_in_session(user_id):
            success = matchmaking_manager.remove_player(self.guild_id, user_id)
            if success:
                await interaction.response.send_message("✅ Вы покинули Matchmaking", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Ошибка при выходе", ephemeral=True)
                return
        else:
            await interaction.response.send_message("❌ Вы не находитесь в Matchmaking", ephemeral=True)
            return

        # Обновляем embed через менеджер для всех, только если сессия еще существует
        session_after = matchmaking_manager.get_session(self.guild_id)
        if session_after:
            bot = interaction.client
            await matchmaking_manager.update_main_embed(self.guild_id, bot)

    async def ready_callback(self, interaction: discord.Interaction):
        """Обработка нажатия кнопки Ready."""
        user_id = interaction.user.id

        session = matchmaking_manager.get_session(self.guild_id)
        if not session:
            await interaction.response.send_message("❌ Сессия не найдена", ephemeral=True)
            return

        if not session.is_player_in_session(user_id):
            await interaction.response.send_message("❌ Вы не находитесь в Matchmaking", ephemeral=True)
            return

        if session.is_player_ready(user_id):
            await interaction.response.send_message("✅ Вы уже готовы!", ephemeral=True)
            return

        session.set_player_ready(user_id, True)

        await interaction.response.send_message("✅ Вы готовы!", ephemeral=True)

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
