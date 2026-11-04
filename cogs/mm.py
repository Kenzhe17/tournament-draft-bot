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

# Канал для турниров
TOURNAMENT_CHANNEL_ID = 1564629925494001744


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

        embed = build_search_embed(search, interaction.guild)
        view = build_search_view(self.bot, search)

        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        search.message_id = message.id
        mm_search_store.set(search)

        logger.info("Создан поиск %s в канале %s", format.value, interaction.channel_id)

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
                            embed = build_search_embed(search, channel.guild)
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
        tournament_channel = self.bot.get_channel(TOURNAMENT_CHANNEL_ID)

        if not tournament_channel:
            logger.error("Канал для турниров не найден: %s", TOURNAMENT_CHANNEL_ID)
            return

        # Определить размер и формат
        tournament_size = TournamentSize.EIGHT if search.format == MMFormat.SQUAD else TournamentSize.EIGHT
        formation_mode = FormationMode.RANDOM

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

        # Добавить игроков в пул
        for player_name in search.players:
            user_id = search.player_user_ids.get(player_name, 0)
            tournament.players_pool.append(player_name)
            tournament.player_user_ids[player_name] = user_id

        # Закрыть регистрацию
        tournament.registration = RegistrationState.CLOSED

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


async def setup(bot: TournamentBot) -> None:
    """Настройка cog."""
    await bot.add_cog(MMCog(bot))
