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
            return

        try:
            channel = bot.get_channel(session.match.main_channel_id)
            if not channel:
                return

            # Ищем последнее сообщение в канале
            async for message in channel.history(limit=10):
                if message.embeds and ("🎮 Matchmaking" in message.embeds[0].title):
                    session.match.main_message_id = message.id
                    return
        except Exception as e:
            logger.error(f"Error finding main message: {e}")

    async def update_main_embed(self, guild_id: int, bot):
        """Обновить embed в главном канале."""
        session = self.get_session(guild_id)
        if not session:
            return

        # Всегда ищем сообщение, чтобы убедиться что оно актуальное
        await self.find_and_set_main_message(guild_id, bot)
        if not session.match.main_message_id:
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

            # Если собрано 8 игроков и драфт еще не начался, запускаем драфт
            if session.is_full() and session.match.phase.name == "searching":
                await self.start_matchmaking_flow(channel, session)
        except Exception as e:
            logger.error(f"Ошибка обновления embed: {e}")
            # Если сообщение не найдено, сбрасываем message_id и пробуем снова
            session.match.main_message_id = None

    async def start_matchmaking_flow(self, channel, session):
        """Начать драфт в главном канале."""
        session.start_draft()

        # Выбираем капитанов по ELO (2 самых высоких ELO)
        await self.select_captains_by_elo(channel, session)

        # Начинаем драфт с Select Menu
        await self.start_draft_selection(channel, session)

    async def select_captains_by_elo(self, channel, session):
        """Выбрать 2 капитана по наивысшему ELO."""
        from storage.player_stats_store import player_stats_store

        # Получаем ELO для всех игроков
        player_elos = []
        for player_id in session.match.players:
            stats = await player_stats_store.get_stats(session.guild_id, player_id)
            elo = stats.elo if stats else 1000  # Default ELO if no stats
            player_elos.append((player_id, elo))

        # Сортируем по ELO (убывание)
        player_elos.sort(key=lambda x: x[1], reverse=True)

        # Выбираем 2 лучших как капитанов
        captain1_id, captain1_elo = player_elos[0]
        captain2_id, captain2_elo = player_elos[1]
        captain1_name = session.match.player_names[captain1_id]
        captain2_name = session.match.player_names[captain2_id]

        session.create_teams(captain1_id, captain1_name, captain2_id, captain2_name)

        embed = discord.Embed(
            title="👑 Капитаны выбраны по ELO",
            color=discord.Color.gold()
        )
        embed.add_field(name=f"Team 1 Captain ({captain1_elo} ELO)", value=captain1_name, inline=True)
        embed.add_field(name=f"Team 2 Captain ({captain2_elo} ELO)", value=captain2_name, inline=True)

        await channel.send(embed=embed)

    async def start_draft_selection(self, channel, session):
        """Начать интерактивный драфт с Select Menu."""
        from storage.player_stats_store import player_stats_store

        # Получаем всех игроков кроме капитанов
        captain_ids = {session.match.teams[0].captain_id, session.match.teams[1].captain_id}
        available_players = [pid for pid in session.match.players if pid not in captain_ids]

        # Получаем ELO для сортировки
        player_elos = []
        for player_id in available_players:
            stats = await player_stats_store.get_stats(session.guild_id, player_id)
            elo = stats.elo if stats else 1000
            player_elos.append((player_id, elo))

        # Сортируем по ELO (убывание)
        player_elos.sort(key=lambda x: x[1], reverse=True)

        # Инициализируем драфт данные
        session.match.draft_data = {
            "available": [pid for pid, _ in player_elos],
            "current_picker": 0,  # 0 = Team 1, 1 = Team 2
            "pick_order": [0, 1, 1, 0, 1, 0],  # Порядок выбора для 6 игроков
            "pick_index": 0,
        }

        # Добавляем капитанов в команды
        session.match.teams[0].players = [session.match.teams[0].captain_id]
        session.match.teams[1].players = [session.match.teams[1].captain_id]

        # Показываем драфт view
        from views.matchmaking_draft_view import MatchmakingDraftView
        view = MatchmakingDraftView(session.guild_id, session)

        embed = discord.Embed(
            title="🎲 Драфт",
            description=f"Капитан {session.match.teams[0].name} выбирает первым.",
            color=discord.Color.blue()
        )

        # Показываем доступных игроков с ELO
        available_text = ""
        for i, (pid, elo) in enumerate(player_elos):
            name = session.match.player_names[pid]
            available_text += f"{i + 1}. {name} ({elo} ELO)\n"

        embed.add_field(name="Доступные игроки:", value=available_text, inline=False)

        message = await channel.send(embed=embed, view=view)

        # Сохраняем message_id для обновления
        session.match.message_id = message.id


# Глобальный экземпляр менеджера
matchmaking_manager = MatchmakingManager()
