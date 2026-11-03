"""Matchmaking tournament commands."""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID
from models.mm_tournament import MMFormat, MMPhase
from storage.mm_store import mm_store
from views.mm_view import MMRegistrationView, create_searching_embed, create_playing_embed

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

MM_MATCH_CHANNEL_ID = 1564629925494001744


class MMCog(commands.Cog):
    """Matchmaking tournament commands."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot
        self._timers: dict[tuple[int, int], asyncio.Task] = {}

    @app_commands.command(name="mm", description="Запустить matchmaking")
    @app_commands.describe(format="Формат матча")
    @app_commands.choices(format=[
        app_commands.Choice(name="duo", value="duo"),
        app_commands.Choice(name="squad", value="squad"),
    ])
    async def mm_create(
        self,
        interaction: discord.Interaction,
        format: str
    ) -> None:
        """Запустить matchmaking."""
        # Проверка на владельца бота
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Эта команда доступна только владельцу бота.",
                ephemeral=True
            )
            return

        # Создать новый турнир (всегда)
        from models.mm_tournament import MMTournament
        format_mode = MMFormat.DUO if format == "duo" else MMFormat.SQUAD
        tournament = MMTournament(
            id="",
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            format=format_mode,
        )

        mm_store.set(tournament)

        # Create embed
        embed = create_searching_embed(tournament)
        view = MMRegistrationView(interaction.guild_id, interaction.channel_id, tournament)

        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        # Update tournament with message_id
        tournament.message_id = message.id
        mm_store.set(tournament)

        # Start 10-minute timer
        self._start_timer(interaction.guild_id, interaction.channel_id, tournament)

    def _start_timer(self, guild_id: int, channel_id: int, tournament) -> None:
        """Start 10-minute timer for matchmaking."""
        async def timer_task():
            try:
                await asyncio.sleep(600)  # 10 minutes

                # Check if tournament still exists and not complete
                tournament = mm_store.get(guild_id, channel_id)
                if not tournament or tournament.phase == MMPhase.COMPLETE:
                    return

                # Cancel matchmaking if not full
                if not tournament.is_full():
                    # Delete tournament
                    mm_store.delete(guild_id, channel_id)

                    # Update message
                    try:
                        channel = self.bot.get_channel(channel_id)
                        if channel:
                            message = await channel.fetch_message(tournament.message_id)
                            embed = discord.Embed(
                                title="⏰ Matchmaking отменен",
                                description="Не набралось достаточно игроков за 10 минут.",
                                color=discord.Color.red()
                            )
                            await message.edit(embed=embed, view=None)
                    except Exception as e:
                        logger.error(f"Error updating timer message: {e}")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error in timer task: {e}")

        task = asyncio.create_task(timer_task())
        self._timers[(guild_id, channel_id)] = task

    def _cancel_timer(self, guild_id: int, channel_id: int) -> None:
        """Cancel timer for a channel."""
        key = (guild_id, channel_id)
        if key in self._timers:
            self._timers[key].cancel()
            del self._timers[key]

    @app_commands.command(name="mmtest", description="Заполнить поиск тестовыми игроками")
    async def mm_test(self, interaction: discord.Interaction) -> None:
        """Заполнить поиск тестовыми игроками."""
        # Проверка на владельца бота
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "❌ Эта команда доступна только владельцу бота.",
                ephemeral=True
            )
            return

        tournament = mm_store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message("❌ Нет активного поиска в этом канале.", ephemeral=True)
            return

        if tournament.phase != MMPhase.SEARCHING:
            await interaction.response.send_message("❌ Поиск уже не в фазе регистрации.", ephemeral=True)
            return

        # Fill to almost full (leave 1 spot)
        target_players = tournament.required_players - 1
        current_players = len(tournament.players)

        if current_players >= target_players:
            await interaction.response.send_message(f"❌ Уже {current_players}/{tournament.required_players} игроков.", ephemeral=True)
            return

        # Add test players with varying skill rating (1.0-10.0)
        sr_values = [9.5, 8.0, 6.5, 5.0, 3.5, 2.0]
        for i in range(target_players - current_players):
            sr = sr_values[i % len(sr_values)]
            tournament.add_player(f"TestPlayer{i+1}", 1000000 + i, sr)

        mm_store.set(tournament)

        # Update embed
        embed = create_searching_embed(tournament)
        view = MMRegistrationView(interaction.guild_id, interaction.channel_id, tournament)

        # Update original message
        try:
            if tournament.message_id:
                channel = interaction.guild.get_channel(tournament.channel_id)
                if channel:
                    message = await channel.fetch_message(tournament.message_id)
                    await message.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating message: {e}")

        await interaction.response.send_message(f"✅ Добавлено {target_players - current_players} тестовых игроков. {len(tournament.players)}/{tournament.required_players}", ephemeral=True)


async def setup(bot: "TournamentBot") -> None:
    """Setup the cog."""
    await bot.add_cog(MMCog(bot))
