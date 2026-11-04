"""Cog для matchmaking системы."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis
from models.mm_search import MMFormat, MMSearch
from models.tournament import FormationMode, RegistrationState, TournamentSize, TournamentPhase, Tournament
from storage.mm_search_store import mm_search_store
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from utils.embeds import build_setup_embed
from views.mm_search_view import build_search_embed, build_search_view

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Каналы для турниров
TOURNAMENT_CHANNEL_IDS = [1549809898643001484, 1563149967303118949, 1563149951033151609]

# Заблокированные каналы для поиска
LOCKED_CHANNELS: dict[int, str] = {}  # channel_id -> search_channel_id


class MMCog(commands.Cog):
    """Cog с командами matchmaking."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot
        self._search_timers: dict[tuple[int, int], asyncio.Task] = {}
        self.bot.mm_cog = self  # Регистрация для доступа из view

    mm_group = app_commands.Group(name="mm", description="Matchmaking поиск")

    @mm_group.command(name="squad", description="Создать поиск Squad (4x4, 8 игроков)")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_squad(self, interaction: discord.Interaction) -> None:
        """Создать поиск для формата Squad."""
        await self._create_search(interaction, MMFormat.SQUAD)

    @mm_group.command(name="delete", description="Удалить все поиски на сервере")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_delete(self, interaction: discord.Interaction) -> None:
        """Удалить все поиски на сервере."""
        all_searches = mm_search_store.get_by_guild(interaction.guild_id)
        deleted_count = 0
        
        for search in all_searches:
            try:
                # Удалить сообщение если есть
                channel = self.bot.get_channel(search.channel_id)
                if channel and search.message_id:
                    try:
                        message = await channel.fetch_message(search.message_id)
                        await message.delete()
                    except Exception:
                        pass
                
                # Удалить из хранилища
                mm_search_store.delete(search.guild_id, search.channel_id)
                deleted_count += 1
                
                # Отменить таймер
                self._cancel_search_timer(search.guild_id, search.channel_id)
            except Exception as e:
                logger.error(f"Error deleting search: {e}")
        
        await interaction.response.send_message(
            f"✅ Удалено {deleted_count} поисков на сервере.",
            ephemeral=True
        )

    @mm_group.command(name="test", description="Заполнить поиск до 7/8 игроков для теста")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_test(self, interaction: discord.Interaction) -> None:
        """Заполнить поиск до 7/8 игроков."""
        search = mm_search_store.get(interaction.guild_id, interaction.channel_id)
        if not search:
            await interaction.response.send_message(
                "❌ В этом канале нет активного поиска. Сначала создайте поиск с /mm squad.",
                ephemeral=True
            )
            return

        required = search.required_players
        current = len(search.players)
        
        if current >= required:
            await interaction.response.send_message(
                "❌ Поиск уже заполнен.",
                ephemeral=True
            )
            return

        # Добавить ботов до 7/8
        target = required - 1  # 7 из 8
        bot_names = [
            "Bot_1", "Bot_2", "Bot_3", "Bot_4", 
            "Bot_5", "Bot_6", "Bot_7", "Bot_8"
        ]
        
        added = 0
        for i in range(target - current):
            if added < len(bot_names):
                bot_name = bot_names[added]
                search.add_player(bot_name, 0)  # user_id = 0 для ботов
                added += 1
        
        mm_search_store.set(search)
        
        # Обновить embed
        embed = await build_search_embed(search, interaction.guild)
        view = build_search_view(self.bot, search)
        try:
            message = await interaction.channel.fetch_message(search.message_id)
            await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating search embed: {e}")
        
        await interaction.response.send_message(
            f"✅ Добавлено {added} ботов. Теперь {len(search.players)}/{required} игроков.",
            ephemeral=True
        )

    async def _create_search(self, interaction: discord.Interaction, format: MMFormat) -> None:
        """Создать новый поиск."""
        # Проверить если уже есть поиск в канале
        existing = mm_search_store.get(interaction.guild_id, interaction.channel_id)
        if existing:
            await interaction.response.send_message(
                "❌ В этом канале уже есть активный поиск.",
                ephemeral=True
            )
            return

        search = MMSearch(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            format=format
        )

        # Сначала отправить ephemeral сообщение
        await interaction.response.send_message("✅ Поиск создан!", ephemeral=True)

        embed = await build_search_embed(search, interaction.guild)
        view = build_search_view(self.bot, search)

        # Отправить вечный embed
        message = await interaction.channel.send(embed=embed, view=view)

        search.message_id = message.id
        mm_search_store.set(search)

        logger.info("Создан поиск %s в канале %s", format.value, interaction.channel_id)
        
        # Запустить таймер
        self._start_search_timer(interaction.guild_id, interaction.channel_id)

    def _start_search_timer(self, guild_id: int, channel_id: int) -> None:
        """Запустить таймер для поиска."""
        async def timer_task():
            try:
                # Уведомление через 15 секунд после старта
                await asyncio.sleep(15)  # 15 секунд
                
                search = mm_search_store.get(guild_id, channel_id)
                if not search:
                    return

                # Не отправлять уведомление если в поиске 0 человек
                if len(search.players) == 0:
                    return

                # Отправить уведомление о поиске в канал уведомлений
                try:
                    from config import NOTIFICATION_CHANNEL_ID, NOTIFICATION_ROLE_ID
                    notification_channel = self.bot.get_channel(NOTIFICATION_CHANNEL_ID)
                    search_channel = self.bot.get_channel(channel_id)
                    
                    if notification_channel and search_channel:
                        current = len(search.players)
                        required = search.required_players
                        
                        role = self.bot.get_guild(guild_id).get_role(NOTIFICATION_ROLE_ID)
                        role_mention = role.mention if role else ""
                        
                        embed = discord.Embed(
                            title="🔍 Поиск игроков",
                            description=f"{search_channel.mention}",
                            color=discord.Color.from_rgb(69, 69, 69)
                        )
                        embed.add_field(name="Игроки", value=f"{current}/{required}", inline=True)
                        
                        await notification_channel.send(
                            content=role_mention,
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions(roles=True)
                        )
                except Exception as e:
                    logger.error(f"Error sending search notification: {e}")

                # Продолжить таймер до 10 минут
                await asyncio.sleep(540)  # Оставшиеся 9 минут

                search = mm_search_store.get(guild_id, channel_id)
                if not search:
                    return

                # Если не заполнен, кикнуть игроков и сбросить канал
                if not search.is_full:
                    player_count = len(search.players)
                    
                    # Разблокировать канал
                    if search.tournament_channel_id in LOCKED_CHANNELS:
                        del LOCKED_CHANNELS[search.tournament_channel_id]
                    
                    # Кикнуть игроков но сохранить поиск активным
                    search.players = []
                    search.player_user_ids = {}
                    search.timer_started = False
                    search.tournament_channel_id = 0
                    mm_search_store.set(search)

                    # Обновить сообщение
                    try:
                        channel = self.bot.get_channel(channel_id)
                        if channel:
                            message = await channel.fetch_message(search.message_id)
                            embed = await build_search_embed(search, channel.guild)
                            view = build_search_view(self.bot, search)
                            await message.edit(embed=embed, view=view)
                            await channel.send(
                                f"⏰ Не набралось достаточно игроков за 10 минут. {player_count} игроков были кикнуты из поиска. Ожидание новых игроков..."
                            )
                    except Exception as e:
                        logger.error(f"Error updating search timer message: {e}")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error in search timer task: {e}")

        task = asyncio.create_task(timer_task())
        self._search_timers[(guild_id, channel_id)] = task

    def _cancel_search_timer(self, guild_id: int, channel_id: int) -> None:
        """Отменить таймер поиска."""
        key = (guild_id, channel_id)
        if key in self._search_timers:
            self._search_timers[key].cancel()
            del self._search_timers[key]

    async def _create_tournament_from_search(self, search: MMSearch, interaction_channel: discord.TextChannel) -> None:
        """Создать обычный турнир из поиска."""
        guild = interaction_channel.guild
        
        # Использовать запомненный канал
        tournament_channel_id = search.tournament_channel_id
        if not tournament_channel_id:
            logger.error("Tournament channel not set for search")
            return

        tournament_channel = self.bot.get_channel(tournament_channel_id)

        if not tournament_channel:
            logger.error("Канал для турниров не найден: %s", tournament_channel_id)
            return

        # Определить размер и формат
        tournament_size = TournamentSize.EIGHT if search.format == MMFormat.SQUAD else TournamentSize.EIGHT
        formation_mode = FormationMode.RANDOM_AUTO  # Использовать random-auto

        # Создать турнир
        tournament = Tournament(
            guild_id=search.guild_id,
            channel_id=tournament_channel.id,
            size=tournament_size,
            formation_mode=formation_mode,
            organizer_id=self.bot.user.id,  # Автоматический
        )

        # Сохранить канал поиска для сброса после завершения
        tournament.mm_search_channel_id = search.channel_id

        # Добавить игроков в пул с их SR
        players_with_sr = []
        for player_name in search.players:
            user_id = search.player_user_ids.get(player_name, 0)
            tournament.players_pool.append(player_name)
            tournament.player_user_ids[player_name] = user_id
            
            # Получить SR игрока
            from storage.player_stats_store import player_stats_store
            stats = await player_stats_store.get(guild.id, user_id)
            sr = stats.skill_rating if stats else 0.0
            players_with_sr.append((player_name, sr))

        # Закрыть регистрацию
        tournament.registration = RegistrationState.CLOSED

        # Распределить игроков по SR для random-auto
        teams = tournament.distribute_players_random_auto(players_with_sr)
        
        # НЕ заполнять круги - команды создаются напрямую
        # Для random-auto круги не используются, только teams

        # Автоматически стартовать турнир и перейти к фазе матчей
        tournament.phase = TournamentPhase.QUALIFIERS
        tournament.is_test = True  # Отключить ставки для random-auto
        tournament.betting_open = False  # Явно отключить ставки
        
        # Создать команды из распределенных игроков
        tournament.teams = []
        for i, team_players in enumerate(teams):
            team_data = {
                "players": team_players,
                "captain": team_players[0] if team_players else None,
                "team_number": i
            }
            tournament.teams.append(team_data)
        
        # Сгенерировать матчи в зависимости от размера
        if tournament_size == TournamentSize.EIGHT:
            # 2 команды - 1 финальный матч
            tournament.qualifier_matches = []
            tournament.final_teams = [0, 1]
            tournament.phase = TournamentPhase.FINAL
        elif tournament_size == TournamentSize.SIXTEEN:
            # 4 команды - 2 полуфинала
            tournament.qualifier_matches = [(0, 1), (2, 3)]
            tournament.qualifier_winners = [None, None]
        else:
            # 32 игрока - 8 команд, 4 отборочных
            tournament.generate_qualifiers()
        
        logger.info("Tournament created with random-auto formation, auto-started and matches generated")

        # Построить embed в зависимости от фазы
        if tournament.phase == TournamentPhase.FINAL:
            from utils.embeds import build_final_embed
            embed = await build_final_embed(tournament, guild)
        elif tournament.phase == TournamentPhase.QUALIFIERS:
            from utils.embeds import build_qualifiers_embed
            embed = await build_qualifiers_embed(tournament, guild)
        elif tournament.phase == TournamentPhase.SEMIFINALS:
            from utils.embeds import build_semifinals_embed
            embed = await build_semifinals_embed(tournament, guild)
        else:
            embed = await build_setup_embed(tournament, guild)
        
        view = self.bot.build_view_for_tournament(tournament)
        self.bot._register_view(view)

        # Отправить в канал турниров
        message = await tournament_channel.send(embed=embed, view=view)
        tournament.message_id = message.id
        store.set(tournament)

        logger.info("Создан турнир из поиска %s в канале %s", search.format.value, tournament_channel.id)

        # Разблокировать канал после создания турнира
        if tournament_channel_id in LOCKED_CHANNELS:
            del LOCKED_CHANNELS[tournament_channel_id]

        # Сбросить поиск
        search.clear()
        mm_search_store.set(search)

        # Обновить embed поиска
        search_embed = await build_search_embed(search, guild)
        search_view = build_search_view(self.bot, search)
        try:
            search_message = await interaction_channel.fetch_message(search.message_id)
            await search_message.edit(embed=search_embed, view=search_view)
        except Exception as e:
            logger.error(f"Error updating search embed: {e}")

    async def reset_search_limit(self, guild_id: int) -> None:
        """Сбросить выбранный канал поиска если освободился турнир."""
        all_searches = mm_search_store.get_by_guild(guild_id)
        for search in all_searches:
            if search.tournament_channel_id:
                # Проверить что канал свободен или турнир завершен
                tournament_channel = self.bot.get_channel(search.tournament_channel_id)
                if tournament_channel:
                    tournament = store.get(guild_id, tournament_channel.id)
                    if not tournament or tournament.phase == TournamentPhase.COMPLETE:
                        # Канал свободен или турнир завершен, разблокировать и сбросить выбор
                        if search.tournament_channel_id in LOCKED_CHANNELS:
                            del LOCKED_CHANNELS[search.tournament_channel_id]
                        search.tournament_channel_id = 0
                        mm_search_store.set(search)
                        
                        # Обновить embed если это необходимо
                        try:
                            channel = self.bot.get_channel(search.channel_id)
                            if channel:
                                message = await channel.fetch_message(search.message_id)
                                embed = await build_search_embed(search, channel.guild)
                                view = build_search_view(self.bot, search)
                                await message.edit(embed=embed, view=view)
                        except Exception as e:
                            logger.error(f"Error resetting search channel: {e}")

    async def on_tournament_complete(self, guild_id: int, channel_id: int) -> None:
        """Вызывается при завершении турнира для разблокировки канала."""
        if channel_id in LOCKED_CHANNELS:
            del LOCKED_CHANNELS[channel_id]
            logger.info(f"Channel {channel_id} unlocked after tournament completion")


async def setup(bot: TournamentBot) -> None:
    """Настройка cog."""
    await bot.add_cog(MMCog(bot))
