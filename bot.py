"""Точка входа — Discord-бот для турнирного драфта."""

from __future__ import annotations

import logging
import sys
import os

import discord
from discord.ext import commands

from config import DATABASE_URL, DISCORD_TOKEN
from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from storage.user_balance_store import user_balance_store
from storage.bet_store import bet_store
from storage.betting_stats_store import betting_stats_store
from utils.embeds import build_embed_for_phase
from views.draft_view import build_draft_view
from views.final_view import FinalView
from views.leaderboard_view import LeaderboardView
from views.matches_view import QualifiersView, SemifinalsView, TeamsView
from views.setup_view import build_setup_view

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class TournamentBot(commands.Bot):
    """Основной класс бота."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            max_ratelimit_timeout=30.0,
            max_ratelimit_retries=5
        )
        self._registered_view_keys: set[str] = set()

    async def setup_hook(self) -> None:
        """Синхронизация slash-команд и восстановление View."""
        # Start betting timer background task
        self.loop.create_task(self.betting_timer_loop())
        # Start draft timer background task
        self.loop.create_task(self.draft_timer_loop())
        
        # Initialize shop items
        try:
            from storage.shop_items import initialize_shop_items
            initialize_shop_items()
            logger.info("Shop items initialized")
        except Exception as e:
            logger.error("Failed to initialize shop items: %s", e)

        # Initialize database if DATABASE_URL is set
        if DATABASE_URL:
            try:
                from storage.db import init_db
                await init_db()
                player_stats_store.enable_db()
                user_balance_store.enable_db()
                bet_store.enable_db()
                betting_stats_store.enable_db()
                logger.info("Database initialized and enabled")
                
                # Sync games from config to database
                try:
                    from storage.games_config import sync_games_to_db
                    synced = await sync_games_to_db(0)  # guild_id not needed for sync
                    logger.info(f"Synced {synced} games from config to database")
                except Exception as e:
                    logger.error(f"Failed to sync games from config: {e}")
            except Exception as e:
                logger.error("Failed to initialize database: %s", e)

        await self.load_extension("cogs.tournament")
        # await self.load_extension("cogs.games.rps")  # Disabled - game in development
        
        # Sync commands globally
        try:
            synced = await self.tree.sync()
            logger.info(f"Slash-команды синхронизированы глобально: {len(synced)} команд")
            for cmd in synced:
                logger.info(f"  - {cmd.name}")
        except Exception as e:
            logger.error(f"Ошибка синхронизации команд: {e}")
        
        # Also sync for specific guilds for faster propagation
        if DATABASE_URL:
            try:
                from storage.db import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    cursor = await conn.fetch("SELECT DISTINCT guild_id FROM players")
                    guild_ids = [row[0] for row in cursor]
                
                for guild_id in guild_ids:
                    try:
                        guild_synced = await self.tree.sync(guild=discord.Object(id=guild_id))
                        logger.info(f"Синхронизировано {len(guild_synced)} команд для сервера {guild_id}")
                    except Exception as e:
                        logger.error(f"Ошибка синхронизации для сервера {guild_id}: {e}")
            except Exception as e:
                logger.error(f"Ошибка при получении серверов из БД: {e}")

        # Store bot instance globally for logging
        from models.tournament import set_bot_instance
        set_bot_instance(self)

        for tournament in store.all():
            view = self.build_view_for_tournament(tournament)
            self._register_view(view)

        # Set up error handler for app commands
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception) -> None:
        """Handle app command errors."""
        logger.error(f"App command error: {error}", exc_info=True)

        # Log to Discord
        if interaction.guild and interaction.user:
            from utils.logging import log_command_error
            command_name = interaction.command.name if interaction.command else "unknown"
            await log_command_error(self, interaction.guild, interaction.user, command_name, str(error))

    def _view_key(self, view: discord.ui.View) -> str:
        """Уникальный ключ View по custom_id его компонентов."""
        ids = sorted(item.custom_id for item in view.children if item.custom_id)
        return "|".join(ids)

    def _register_view(self, view: discord.ui.View | None) -> None:
        """Зарегистрировать persistent View (без дубликатов)."""
        if view is None:
            return
        key = self._view_key(view)
        # Always add the view - Discord handles replacements
        self.add_view(view)
        if key not in self._registered_view_keys:
            self._registered_view_keys.add(key)
        logger.debug("Зарегистрирован View: %s", key)

    def build_view_for_tournament(
        self, tournament: Tournament
    ) -> discord.ui.View | None:
        """Построить View в зависимости от фазы турнира."""
        phase = tournament.phase
        gid = tournament.guild_id

        if phase == TournamentPhase.SETUP:
            return build_setup_view(tournament)

        if phase == TournamentPhase.DRAFT:
            return build_draft_view(tournament)

        if phase == TournamentPhase.TEAMS:
            return TeamsView(gid, tournament)

        if phase == TournamentPhase.QUALIFIERS:
            return QualifiersView(
                gid,
                tournament.qualifier_matches,
                tournament.qualifier_winners,
                tournament,
            )

        if phase == TournamentPhase.SEMIFINALS:
            return SemifinalsView(
                gid,
                tournament.semifinal_matches,
                tournament.semifinal_winners,
                tournament,
            )

        if phase == TournamentPhase.FINAL:
            return FinalView(gid, tournament.final_teams, tournament)

        if phase == TournamentPhase.COMPLETE:
            return None  # No buttons needed for completed tournament

        return None

    async def update_tournament_message(
        self, guild: discord.Guild, tournament: Tournament
    ) -> None:
        """Отредактировать главное сообщение турнира."""
        if not tournament.message_id:
            logger.warning("Нет message_id для сервера %s", guild.id)
            return

        channel = guild.get_channel(tournament.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(tournament.channel_id)
            except discord.HTTPException as exc:
                logger.error("Канал не найден: %s", exc)
                return

        try:
            message = await channel.fetch_message(tournament.message_id)
        except discord.HTTPException as exc:
            logger.error("Сообщение не найдено: %s", exc)
            return

        embed = await build_embed_for_phase(tournament, guild)
        view = self.build_view_for_tournament(tournament)
        self._register_view(view)

        try:
            await message.edit(embed=embed, view=view)
        except discord.HTTPException as exc:
            logger.error("Не удалось обновить сообщение: %s", exc)

    async def betting_timer_loop(self) -> None:
        """Background task to update betting timer in tournament messages."""
        import asyncio
        # Track which tournaments we've updated after betting closed
        updated_after_close = set()
        
        while not self.is_closed():
            try:
                await asyncio.sleep(5)  # Update every 5 seconds
                
                # Get all guilds with active tournaments
                for guild in self.guilds:
                    tournament = store.get(guild.id)
                    if not tournament:
                        continue
                    
                    tournament_key = f"{guild.id}_{tournament.betting_phase}"
                    
                    if tournament.is_betting_open():
                        # Update message to show countdown
                        await self.update_tournament_message(guild, tournament)
                        # Reset the updated flag since betting is open again
                        if tournament_key in updated_after_close:
                            updated_after_close.remove(tournament_key)
                    elif tournament.betting_phase_start_time is not None:
                        # Betting was open but now closed - update once to remove timer
                        if tournament_key not in updated_after_close:
                            await self.update_tournament_message(guild, tournament)
                            updated_after_close.add(tournament_key)
            except Exception as e:
                logger.error(f"Error in betting timer loop: {e}", exc_info=True)

    async def draft_timer_loop(self) -> None:
        """Background task to handle draft pick timer and auto-random picks."""
        import asyncio

        while not self.is_closed():
            try:
                await asyncio.sleep(5)  # Check every 5 seconds

                # Get all guilds with active tournaments
                for guild in self.guilds:
                    tournament = store.get(guild.id)
                    if not tournament:
                        continue

                    # Only process if in draft phase
                    if tournament.phase != TournamentPhase.DRAFT:
                        continue

                    # Check if current picker exists
                    picker_pos = tournament.current_picker_position()
                    if picker_pos is None:
                        continue

                    # Check if time has expired
                    remaining_time = tournament.get_draft_pick_remaining_time()
                    if remaining_time > 0:
                        # Update message to show countdown every 5 seconds
                        await self.update_tournament_message(guild, tournament)
                    else:
                        # Time expired - make random pick
                        logger.info(f"Draft timer expired for guild {guild.id}, making random pick")
                        random_pick = tournament.pick_random_player()
                        if random_pick:
                            picker_pos, player = random_pick
                            tournament.pick_player(picker_pos, player)
                            draft_complete = tournament.advance_after_pick()
                            store.set(tournament)
                            
                            # Update tournament message
                            await self.update_tournament_message(guild, tournament)
                            
                            # Delete old draft message if exists
                            if tournament.draft_message_id > 0:
                                try:
                                    channel = guild.get_channel(tournament.channel_id)
                                    if channel:
                                        old_message = await channel.fetch_message(tournament.draft_message_id)
                                        await old_message.delete()
                                except Exception:
                                    pass
                            
                            # Send new draft message with next captain ping if draft not complete
                            if not draft_complete:
                                next_picker_pos = tournament.current_picker_position()
                                if next_picker_pos is not None:
                                    next_captain_name = tournament.captains[tournament.captain_order[next_picker_pos]]
                                    next_captain_id = tournament.player_user_ids.get(next_captain_name, 0)
                                    channel = guild.get_channel(tournament.channel_id)
                                    if channel:
                                        if next_captain_id > 0:
                                            new_message = await channel.send(f"⏱️ Время вышло! Случайный выбор: {player} был выбран.\n➡️ <@{next_captain_id}> - ваша очередь выбирать!")
                                        else:
                                            new_message = await channel.send(f"⏱️ Время вышло! Случайный выбор: {player} был выбран.\n➡️ {next_captain_name} - ваша очередь выбирать!")
                                        tournament.draft_message_id = new_message.id
                                        store.set(tournament)
                            else:
                                # Draft complete
                                logger.info(f"Draft completed automatically for guild {guild.id}")
            except Exception as e:
                logger.error(f"Error in draft timer loop: {e}", exc_info=True)

    async def on_ready(self) -> None:
        logger.info("Бот запущен как %s (ID: %s)", self.user, self.user.id)
        
        # Debug: Log all registered commands
        commands = list(self.tree.walk_commands())
        logger.info(f"Зарегистрировано {len(commands)} команд:")
        for cmd in commands:
            cmd_type = cmd.type.__name__ if hasattr(cmd, 'type') else 'Group'
            logger.info(f"  - /{cmd.name} (type: {cmd_type})")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Log when bot is added to a server."""
        from utils.logging import log_guild_join
        await log_guild_join(self, guild)


def main() -> None:
    """Запуск бота."""
    if not DISCORD_TOKEN:
        logger.error(
            "DISCORD_TOKEN не задан. Скопируйте .env.example в .env и укажите токен."
        )
        sys.exit(1)

    bot = TournamentBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
