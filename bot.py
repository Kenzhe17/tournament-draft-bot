"""Точка входа — Discord-бот для турнирного драфта."""

from __future__ import annotations

import logging
import sys
import os

import discord
from discord.ext import commands

from config import DATABASE_URL, DISCORD_TOKEN, replace_emojis
from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from storage.user_balance_store import user_balance_store
from storage.bet_store import bet_store
from storage.betting_stats_store import betting_stats_store
from storage.warn_store import warn_store
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
        intents.message_content = True
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
        # Start draft timer background task (for auto-random picks)
        self.loop.create_task(self.draft_timer_loop())
        # Start pet hunger check background task
        try:
            self.loop.create_task(self.pet_hunger_check_loop())
        except Exception as e:
            logger.error(f"Failed to create pet hunger check loop task: {e}", exc_info=True)
        
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
                from storage.bet_store import bet_store
                await init_db()
                player_stats_store.enable_db()
                user_balance_store.enable_db()
                bet_store.enable_db()
                betting_stats_store.enable_db()
                warn_store.enable_db()
                logger.info("Database initialized and enabled")
                
                # Initialize warnings table
                try:
                    await warn_store.init_table()
                    logger.info("Warnings table initialized")
                except Exception as e:
                    logger.error(f"Failed to initialize warnings table: {e}", exc_info=True)
                
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
        await self.load_extension("cogs.games.coin_flip")
        await self.load_extension("cogs.games.rps")
        await self.load_extension("cogs.games.dice_roll")
        await self.load_extension("cogs.games.guess_number")
        await self.load_extension("cogs.games.lottery")
        await self.load_extension("cogs.games.high_low")
        await self.load_extension("cogs.games.triple_chance")
        await self.load_extension("cogs.games.dice_bet")
        await self.load_extension("cogs.games.roulette")
        await self.load_extension("cogs.games.craps")
        await self.load_extension("cogs.games.snap")
        await self.load_extension("cogs.rob")
        await self.load_extension("cogs.bank")
        await self.load_extension("cogs.math_quiz")
        await self.load_extension("cogs.sell")
        await self.load_extension("cogs.thread")
        await self.load_extension("cogs.coin_drop")
        await self.load_extension("cogs.pet")
        await self.load_extension("cogs.isolate")
        await self.load_extension("cogs.br")
        await self.load_extension("cogs.team")
        await self.load_extension("cogs.bot_settings")
        await self.load_extension("cogs.moderation")

        # Load teams from PostgreSQL after extensions are loaded
        from storage.team_store import team_store
        await team_store.load_from_postgres()

        # Load betting odds from PostgreSQL after extensions are loaded
        try:
            from storage.bet_store import bet_store
            await bet_store.load_from_db()
        except Exception as e:
            logger.error(f"Failed to load betting odds from DB: {e}", exc_info=True)

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
                    cursor = await conn.fetch("SELECT DISTINCT guild_id FROM player_stats")
                    guild_ids = [row[0] for row in cursor]

                for guild_id in guild_ids:
                    try:
                        guild_synced = await self.tree.sync(guild=discord.Object(id=guild_id))
                        logger.info(f"Синхронизировано {len(guild_synced)} команд для сервера {guild_id}")
                    except Exception as e:
                        logger.error(f"Ошибка синхронизации для сервера {guild_id}: {e}")
            except Exception as e:
                logger.error(f"Ошибка при получении серверов из БД: {e}")

        # Force sync for main guild to ensure commands are available immediately
        MAIN_GUILD_ID = 1196165488565289012
        try:
            guild_synced = await self.tree.sync(guild=discord.Object(id=MAIN_GUILD_ID))
            logger.info(f"Синхронизировано {len(guild_synced)} команд для основного сервера {MAIN_GUILD_ID}")
        except Exception as e:
            logger.error(f"Ошибка синхронизации для основного сервера: {e}")

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
                tournament.channel_id,
                tournament.qualifier_matches,
                tournament.qualifier_winners,
                tournament,
            )

        if phase == TournamentPhase.SEMIFINALS:
            return SemifinalsView(
                gid,
                tournament.channel_id,
                tournament.semifinal_matches,
                tournament.semifinal_winners,
                tournament,
            )

        if phase == TournamentPhase.FINAL:
            return FinalView(gid, tournament.channel_id, tournament.final_teams, tournament)

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
            logger.warning("Сообщение не найдено (возможно удалено): %s", exc)
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
                    # Get all tournaments for this guild
                    tournaments = store.get_by_guild(guild.id)
                    for tournament in tournaments:
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
                    # Get all tournaments for this guild
                    tournaments = store.get_by_guild(guild.id)
                    for tournament in tournaments:
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
                                            new_message = await channel.send(f"⏱️ Время вышло! Случайный выбор: {player} был выбран.\n{replace_emojis('white_arrow')} <@{next_captain_id}> - ваша очередь выбирать!")
                                        else:
                                            new_message = await channel.send(f"⏱️ Время вышло! Случайный выбор: {player} был выбран.\n{replace_emojis('white_arrow')} {next_captain_name} - ваша очередь выбирать!")
                                        tournament.draft_message_id = new_message.id
                                        store.set(tournament)
                            else:
                                # Draft complete
                                logger.info(f"Draft completed automatically for guild {guild.id}")
                                from utils.logging import log_draft_completed
                                await log_draft_completed(self, guild)
            except Exception as e:
                logger.error(f"Error in draft timer loop: {e}", exc_info=True)

    async def pet_hunger_check_loop(self) -> None:
        """Background task to check pet hunger and send notifications."""
        import asyncio


        while not self.is_closed():
            try:
    
                await asyncio.sleep(300)  # Check every 5 minutes

                
                # Check if bot is still connected
                if not self.is_ready():
                    continue

                # Get all guilds
                for guild in self.guilds:
                    try:
                        from storage.pet_store import pet_store
                        from storage.db import get_pool

                        pool = await get_pool()
                        async with pool.acquire() as conn:
                            # Get only pets from this guild
                            rows = await conn.fetch("SELECT * FROM pets WHERE owner_1_id = ANY($1) OR owner_2_id = ANY($1)", 
                                                    [member.id for member in guild.members])

                            for row in rows:
                                pet = pet_store._row_to_pet(row)
                                # Check hunger for each pet
                                await pet_store.check_fullness_and_energy(pet.pet_id, guild.id, self)
                    except Exception as e:
                        logger.error(f"Error in pet hunger check for guild {guild.id}: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"Error in pet hunger check loop: {e}", exc_info=True)

    async def on_ready(self) -> None:
        logger.info("Бот запущен как %s (ID: %s)", self.user, self.user.id)

        # Sync commands with Discord
        try:
            synced = await self.tree.sync()
            logger.info(f"Синхронизировано {len(synced)} slash-команд")
        except Exception as e:
            logger.error(f"Ошибка синхронизации команд: {e}")

        # Restore org role view if it exists
        from views.org_role_view import load_state, OrgRoleView
        state = load_state()
        if state.message_id > 0 and state.channel_id > 0 and state.guild_id > 0:
            try:
                guild = self.get_guild(state.guild_id)
                if guild:
                    channel = guild.get_channel(state.channel_id)
                    if channel:
                        try:
                            message = await channel.fetch_message(state.message_id)
                            view = OrgRoleView(state)
                            self.add_view(view, message_id=state.message_id)
                            logger.info("Org role view restored")
                        except discord.NotFound:
                            logger.warning("Org role message not found, will be recreated")
            except Exception as e:
                logger.error(f"Error restoring org role view: {e}")

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

    async def on_member_join(self, member: discord.Member) -> None:
        """Send welcome message when a member joins the server."""
        # Check if welcome is enabled in settings
        from storage.json_store import store
        settings = store.get_settings()
        if not settings.get("welcome_enabled", True):
            return

        WELCOME_CHANNEL_ID = 1200125075156910181

        # Only send for the specific server
        if member.guild.id != 1196165488565289012:
            return

        # Get the welcome channel
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if not channel:
            try:
                channel = await member.guild.fetch_channel(WELCOME_CHANNEL_ID)
            except discord.HTTPException:
                logger.error(f"Welcome channel {WELCOME_CHANNEL_ID} not found")
                return

        # Calculate member count (current count + 1 for the new member)
        member_count = member.guild.member_count + 1

        # Send plain text welcome message
        await channel.send(
            f"₊˚⊹♡ **Добро пожаловать на сервер**, {member.mention}!\n\n"
            f"ты стал(а) нашим **{member_count:,}-м** участником ♡\n"
            f"чувствуй себя как дома, поприветствуй всех и заглядывай в чат!\n\n"
            f"୨୧ надеемся, тебе у нас понравится ୨୧"
        )


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
