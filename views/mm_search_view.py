"""View для matchmaking поиска."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis, get_emoji
from models.mm_search import MMFormat, MMSearch
from models.tournament import TournamentPhase
from storage.mm_search_store import mm_search_store
from storage.json_store import store
from storage.player_stats_store import player_stats_store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


async def build_search_embed(search: MMSearch, guild: discord.Guild) -> discord.Embed:
    """Построить embed для поиска."""
    format_name = "Duo (2x2)" if search.format == MMFormat.DUO else "БО (4x4)"
    required = search.required_players
    current = len(search.players)

    embed = discord.Embed(
        title=replace_emojis("a_star winner " + format_name + " | ПОИСК"),
        description=replace_emojis("white_dot Информация о поиске:\n"
                                  "sub_middle Формат: " + format_name + "\n"
                                  "sub_middle Игроки: " + str(current) + "/" + str(required) + "\n"
                                  "sub_directory Таймер: " + ('Запущен' if search.timer_started else 'Ожидание первого игрока') + "\n\n"
                                  "white_dot Игроки в поиске:\n\n"
                                  "Нажмите кнопку ниже чтобы войти в поиск"),
        color=discord.Color.from_rgb(69, 69, 69)
    )

    # Добавить игроков с их SR
    if search.players:
        player_info = []
        for player_name in search.players:
            user_id = search.player_user_ids.get(player_name, 0)
            if user_id > 0:
                stats = await player_stats_store.get(guild.id, user_id)
                sr = stats.skill_rating if stats else 1.0
                player_info.append(replace_emojis("white_arrow " + player_name + " (SR: " + f"{sr:.1f}" + ")"))
            else:
                player_info.append(replace_emojis("white_arrow " + player_name))
        embed.add_field(name="", value="\n".join(player_info), inline=False)

    return embed


class JoinSearchButton(discord.ui.Button):
    """Кнопка для входа в поиск."""

    def __init__(self, bot: TournamentBot, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Войти в поиск",
            emoji="🔍",
            custom_id=f"mm_join:{guild_id}:{channel_id}"
        )
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        search = mm_search_store.get(self.guild_id, self.channel_id)
        if not search:
            await interaction.response.send_message("❌ Поиск не найден.", ephemeral=True)
            return

        if search.is_full:
            await interaction.response.send_message("❌ Поиск уже заполнен.", ephemeral=True)
            return

        # Проверка на активный турнир
        all_tournaments = store.get_by_guild(self.guild_id)
        for tournament in all_tournaments:
            if tournament.is_player_active(interaction.user.id):
                await interaction.response.send_message(
                    "❌ Вы уже участвуете в активном турнире. Завершите его чтобы войти в поиск.",
                    ephemeral=True
                )
                return

        # Проверка на активный поиск
        all_searches = mm_search_store.get_by_guild(self.guild_id)
        for other_search in all_searches:
            if other_search.channel_id != self.channel_id:
                if interaction.user.id in other_search.player_user_ids.values():
                    await interaction.response.send_message(
                        "❌ Вы уже в другом поиске. Выйдите из него сначала.",
                        ephemeral=True
                    )
                    return

        # Если это первый игрок - выбрать канал для турнира
        if not search.tournament_channel_id:
            # Проверить канал 1
            channel_1 = self.bot.get_channel(1549809898643001484)
            if channel_1:
                tournament_1 = store.get(self.guild_id, channel_1.id)
                if not tournament_1 or tournament_1.phase == TournamentPhase.COMPLETE:
                    search.tournament_channel_id = channel_1.id
                    mm_search_store.set(search)
                else:
                    # Канал 1 занят, проверить канал 2
                    channel_2 = self.bot.get_channel(1567611808884465814)
                    if channel_2:
                        tournament_2 = store.get(self.guild_id, channel_2.id)
                        if not tournament_2 or tournament_2.phase == TournamentPhase.COMPLETE:
                            search.tournament_channel_id = channel_2.id
                            mm_search_store.set(search)
                        else:
                            # Оба канала заняты
                            await interaction.response.send_message(
                                "❌ Все каналы для турниров заняты. Ожидайте завершения одного из турниров.",
                                ephemeral=True
                            )
                            return
                    else:
                        await interaction.response.send_message("❌ Канал для турниров не найден.", ephemeral=True)
                        return
            else:
                await interaction.response.send_message("❌ Канал для турниров не найден.", ephemeral=True)
                return
        else:
            # Если канал уже выбран, проверить что он свободен
            tournament_channel = self.bot.get_channel(search.tournament_channel_id)
            if tournament_channel:
                tournament = store.get(self.guild_id, tournament_channel.id)
                if tournament and tournament.phase != TournamentPhase.COMPLETE:
                    # Канал занят, сбросить выбор
                    search.tournament_channel_id = 0
                    mm_search_store.set(search)
                    await interaction.response.send_message(
                        "❌ Канал для турниров занят. Повторите попытку.",
                        ephemeral=True
                    )
                    return

        # Добавить игрока
        player_name = interaction.user.display_name
        if not search.add_player(player_name, interaction.user.id):
            await interaction.response.send_message("❌ Не удалось добавить в поиск.", ephemeral=True)
            return

        # Запустить таймер если первый игрок
        if not search.timer_started:
            search.timer_started = True
            mm_search_store.set(search)
            # Запустить таймер в cog
            if hasattr(self.bot, 'mm_cog'):
                self.bot.mm_cog._start_search_timer(self.guild_id, self.channel_id)

        mm_search_store.set(search)

        # Проверить если поиск заполнен - создать турнир
        if search.is_full:
            # Отменить таймер
            if hasattr(self.bot, 'mm_cog'):
                self.bot.mm_cog._cancel_search_timer(self.guild_id, self.channel_id)

            # Создать турнир
            if hasattr(self.bot, 'mm_cog'):
                await self.bot.mm_cog._create_tournament_from_search(search, interaction.channel)
            else:
                await interaction.response.send_message("❌ Ошибка системы MM.", ephemeral=True)
                return

        # Обновить embed
        embed = await build_search_embed(search, interaction.guild)
        view = build_search_view(self.bot, search)
        await interaction.response.edit_message(embed=embed, view=view)


class LeaveSearchButton(discord.ui.Button):
    """Кнопка для выхода из поиска."""

    def __init__(self, bot: TournamentBot, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти",
            emoji="❌",
            custom_id=f"mm_leave:{guild_id}:{channel_id}"
        )
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        search = mm_search_store.get(self.guild_id, self.channel_id)
        if not search:
            await interaction.response.send_message("❌ Поиск не найден.", ephemeral=True)
            return

        player_name = interaction.user.display_name
        if not search.remove_player(player_name):
            await interaction.response.send_message("❌ Вы не в поиске.", ephemeral=True)
            return

        # Если не осталось игроков, сбросить таймер
        if not search.players:
            search.timer_started = False
            if hasattr(self.bot, 'mm_cog'):
                self.bot.mm_cog._cancel_search_timer(self.guild_id, self.channel_id)

        mm_search_store.set(search)

        # Обновить embed
        embed = await build_search_embed(search, interaction.guild)
        view = build_search_view(self.bot, search)
        await interaction.response.edit_message(embed=embed, view=view)


def build_search_view(bot: TournamentBot, search: MMSearch) -> discord.ui.View:
    """Построить view для поиска."""
    view = discord.ui.View(timeout=None)
    view.add_item(JoinSearchButton(bot, search.guild_id, search.channel_id))
    view.add_item(LeaveSearchButton(bot, search.guild_id, search.channel_id))
    return view
