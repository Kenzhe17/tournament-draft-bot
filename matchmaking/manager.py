"""Matchmaking manager for handling matchmaking sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from matchmaking.session import MatchmakingSession

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


class MatchmakingManager:
    """Менеджер для управления matchmaking сессиями."""

    def __init__(self):
        self.sessions: dict[int, MatchmakingSession] = {}  # guild_id -> session
        self.player_sessions: dict[int, str] = {}  # user_id -> match_id

    def get_session(self, guild_id: int) -> MatchmakingSession | None:
        """Получить активную сессию для сервера."""
        return self.sessions.get(guild_id)

    def create_session(self, guild_id: int, main_channel_id: int) -> MatchmakingSession:
        """Создать новую сессию matchmaking."""
        session = MatchmakingSession(guild_id, main_channel_id)
        session.match.main_channel_id = main_channel_id
        self.sessions[guild_id] = session
        logger.info(f"Created matchmaking session {session.match_id} for guild {guild_id}")
        return session

    def delete_session(self, guild_id: int) -> None:
        """Удалить сессию matchmaking."""
        if guild_id in self.sessions:
            session = self.sessions[guild_id]
            # Удаляем ссылки на игроков
            for player_id in session.match.players:
                if player_id in self.player_sessions:
                    del self.player_sessions[player_id]
            del self.sessions[guild_id]
            logger.info(f"Deleted matchmaking session for guild {guild_id}")

    def reset_session(self, guild_id: int) -> None:
        """Сбросить сессию для нового матча (сохраняя main_message_id)."""
        if guild_id not in self.sessions:
            return

        session = self.sessions[guild_id]
        main_message_id = session.match.main_message_id
        main_channel_id = session.match.main_channel_id

        # Удаляем ссылки на игроков
        for player_id in session.match.players:
            if player_id in self.player_sessions:
                del self.player_sessions[player_id]

        # Создаем новую сессию с теми же параметрами
        new_session = MatchmakingSession(guild_id, main_channel_id)
        new_session.match.main_message_id = main_message_id
        self.sessions[guild_id] = new_session

        logger.info(f"Reset matchmaking session for guild {guild_id}")

    def add_player(self, guild_id: int, user_id: int, user_name: str, channel_id: int = 1521101891235221594) -> tuple[bool, str]:
        """Добавить игрока в matchmaking. Возвращает (success, message)."""
        # Проверяем, есть ли активная сессия
        session = self.get_session(guild_id)
        if not session:
            session = self.create_session(guild_id, channel_id)

        # Проверяем, не находится ли игрок уже в сессии
        if session.is_player_in_session(user_id):
            return False, "Вы уже участвуете в Matchmaking"

        # Проверяем, не находится ли игрок в другой сессии
        if user_id in self.player_sessions:
            return False, "Вы уже участвуете в другой сессии"

        # Проверяем, полная ли сессия
        if session.is_full():
            return False, "Лобби заполнено"

        # Добавляем игрока
        if session.add_player(user_id, user_name):
            self.player_sessions[user_id] = session.match_id
            return True, f"Вы добавлены в очередь ({session.get_player_count()}/8)"

        return False, "Не удалось добавить игрока"

    def remove_player(self, guild_id: int, user_id: int) -> bool:
        """Удалить игрока из matchmaking."""
        session = self.get_session(guild_id)
        if not session:
            return False

        if session.remove_player(user_id):
            if user_id in self.player_sessions:
                del self.player_sessions[user_id]

            # Если сессия пуста, удаляем её
            if session.get_player_count() == 0:
                self.delete_session(guild_id)

            return True

        return False

    def get_player_session(self, user_id: int) -> MatchmakingSession | None:
        """Получить сессию игрока по user_id."""
        match_id = self.player_sessions.get(user_id)
        if not match_id:
            return None

        for session in self.sessions.values():
            if session.match_id == match_id:
                return session
        return None

    def is_player_in_matchmaking(self, user_id: int) -> bool:
        """Проверить, находится ли игрок в matchmaking."""
        return user_id in self.player_sessions

    async def update_main_embed(self, guild_id: int, bot):
        """Обновить embed в главном канале."""
        session = self.get_session(guild_id)
        if not session or not session.match.main_message_id:
            return

        try:
            channel = bot.get_channel(session.match.main_channel_id)
            if not channel:
                return

            message = await channel.fetch_message(session.match.main_message_id)

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

            await message.edit(embed=embed)
        except Exception as e:
            logger.error(f"Ошибка обновления embed: {e}")


# Глобальный экземпляр менеджера
matchmaking_manager = MatchmakingManager()
