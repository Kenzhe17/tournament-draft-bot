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
from models.tournament import FormationMode, RegistrationState, TournamentSize, TournamentPhase
from storage.mm_search_store import mm_search_store
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from utils.embeds import build_setup_embed
from views.mm_search_view import build_search_embed, build_search_view

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Каналы для турниров
TOURNAMENT_CHANNEL_ID = 1549809898643001484
TOURNAMENT_CHANNEL_ID_2 = 1567611808884465814


class MMCog(commands.Cog):
    """Cog с командами matchmaking."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot
        self._search_timers: dict[tuple[int, int], asyncio.Task] = {}
        self.bot.mm_cog = self  # Регистрация для доступа из view

    mm_group = app_commands.Group(name="mm", description="Matchmaking поиск")

    @mm_group.command(name="duo", description="Создать поиск Duo (2x2, 4 игрока)")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_duo(self, interaction: discord.Interaction) -> None:
        """Создать поиск для формата Duo."""
        await self._create_search(interaction, MMFormat.DUO)

    @mm_group.command(name="squad", description="Создать поиск Squad (4x4, 8 игроков)")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_squad(self, interaction: discord.Interaction) -> None:
        """Создать поиск для формата Squad."""
        await self._create_search(interaction, MMFormat.SQUAD)

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
                await asyncio.sleep(600)  # 10 минут

                search = mm_search_store.get(guild_id, channel_id)
                if not search:
                    return

                # Если не заполнен, сбросить игроков
                if not search.is_full:
                    player_count = len(search.players)
                    search.clear()
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
                                f"⏰ Не набралось достаточно игроков за 10 минут. {player_count} игроков были кикнуты из поиска."
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
        
        # Заполнить круги из распределенных команд
        tournament.circle1 = [team[0] for team in teams]  # Капитаны
        if len(teams[0]) > 1:
            tournament.circle2 = [team[1] for team in teams]
        if len(teams[0]) > 2:
            tournament.circle3 = [team[2] for team in teams]
        if len(teams[0]) > 3:
            tournament.circle4 = [team[3] for team in teams]

        # Автоматически стартовать турнир
        tournament.phase = TournamentPhase.TEAMS
        tournament.is_test = True  # Отключить ставки для random-auto
        
        logger.info("Tournament created with random-auto formation and auto-started")

        # Построить embed
        embed = await build_setup_embed(tournament, guild)
        view = self.bot.build_view_for_tournament(tournament)
        self.bot._register_view(view)

        # Отправить в канал турниров
        message = await tournament_channel.send(embed=embed, view=view)
        tournament.message_id = message.id
        store.set(tournament)

        logger.info("Создан турнир из поиска %s в канале %s", search.format.value, tournament_channel.id)

        # Сбросить поиск
        search.clear()
        mm_search_store.set(search)

        # Обновить embed поиска
        search_embed = build_search_embed(search, guild)
        search_view = build_search_view(self.bot, search)
        try:
            search_message = await interaction_channel.fetch_message(search.message_id)
            await search_message.edit(embed=search_embed, view=search_view)
        except Exception as e:
            logger.error(f"Error updating search embed: {e}")

        # Уведомить в канале поиска
        await interaction_channel.send(
            f"✅ Турнир создан! Перейдите в канал {tournament_channel.mention} для игры."
        )

    async def reset_search_limit(self, guild_id: int) -> None:
        """Сбросить выбранный канал поиска если освободился турнир."""
        all_searches = mm_search_store.get_by_guild(guild_id)
        for search in all_searches:
            if search.tournament_channel_id:
                # Проверить что канал свободен
                tournament_channel = self.bot.get_channel(search.tournament_channel_id)
                if tournament_channel:
                    tournament = store.get(guild_id, tournament_channel.id)
                    if not tournament or tournament.phase == TournamentPhase.COMPLETE:
                        # Канал свободен, сбросить выбор
                        search.tournament_channel_id = 0
                        mm_search_store.set(search)
                        
                        # Обновить embed если это необходимо
                        try:
                            channel = self.bot.get_channel(search.channel_id)
                            if channel:
                                message = await channel.fetch_message(search.message_id)
                                embed = build_search_embed(search, channel.guild)
                                view = build_search_view(self.bot, search)
                                await message.edit(embed=embed, view=view)
                        except Exception as e:
                            logger.error(f"Error resetting search channel: {e}")


async def setup(bot: TournamentBot) -> None:
    """Настройка cog."""
    await bot.add_cog(MMCog(bot))
