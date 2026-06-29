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
            # Если сессия создана через Join, нужно найти существующее сообщение
            # Это будет обработано в update_main_embed

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

    async def find_and_set_main_message(self, guild_id: int, bot):
        """Найти последнее сообщение с matchmaking embed."""
        session = self.get_session(guild_id)
        if not session:
            logger.warning(f"No session found for guild {guild_id}")
            return

        try:
            channel = bot.get_channel(session.match.main_channel_id)
            if not channel:
                logger.warning(f"Channel {session.match.main_channel_id} not found for guild {guild_id}")
                return

            logger.info(f"Searching for matchmaking message in channel {session.match.main_channel_id}")
            # Ищем последнее сообщение в канале
            async for message in channel.history(limit=10):
                if message.embeds:
                    logger.info(f"Found embed with title: {message.embeds[0].title}")
                    if "🎮 Matchmaking" in message.embeds[0].title:
                        session.match.main_message_id = message.id
                        logger.info(f"Found matchmaking message {message.id} for guild {guild_id}")
                        return
            logger.warning(f"No matchmaking message found in last 10 messages for guild {guild_id}")
        except Exception as e:
            logger.error(f"Error finding main message: {e}")

    async def update_main_embed(self, guild_id: int, bot):
        """Обновить embed в главном канале."""
        session = self.get_session(guild_id)
        if not session:
            logger.warning(f"No session found for guild {guild_id} in update_main_embed")
            return

        logger.info(f"Updating embed for guild {guild_id}, player count: {session.get_player_count()}")
        logger.info(f"main_message_id: {session.match.main_message_id}, main_channel_id: {session.match.main_channel_id}")

        # Всегда ищем сообщение, чтобы убедиться что оно актуальное
        logger.info("Searching for current matchmaking message...")
        await self.find_and_set_main_message(guild_id, bot)
        if not session.match.main_message_id:
            logger.warning("No matchmaking message found, cannot update embed")
            return

        try:
            channel = bot.get_channel(session.match.main_channel_id)
            if not channel:
                logger.warning(f"Channel {session.match.main_channel_id} not found")
                return

            message = await channel.fetch_message(session.match.main_message_id)
            logger.info(f"Found message {message.id}, updating embed")

            player_count = session.get_player_count()
            player_names = [session.match.player_names.get(pid, "Unknown") for pid in session.match.players]

            embed = discord.Embed(
                title="🎮 Matchmaking Lobby",
                color=discord.Color.blue()
            )

            if session.is_full():
                embed.description = "🎉 **Match Found!**\n\n8/8 игроков собрано."
                # Если собрано 8 игроков, начинаем драфт
                await self.start_matchmaking_flow(channel, session)
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
            logger.info(f"Successfully updated embed for guild {guild_id}")
        except Exception as e:
            logger.error(f"Ошибка обновления embed: {e}", exc_info=True)
            # Если сообщение не найдено, сбрасываем message_id и пробуем снова
            session.match.main_message_id = None
            logger.info("Reset main_message_id due to error, will search again next time")

    async def start_matchmaking_flow(self, channel, session):
        """Начать драфт в главном канале."""
        session.start_draft()

        # Отправляем сообщение о начале матча
        await self.send_match_start_message(channel, session)

        # Выбираем капитанов
        await self.select_captains(channel, session)

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


# Глобальный экземпляр менеджера
matchmaking_manager = MatchmakingManager()
