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
from utils.design import compose
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
        # Start birthday greeting background task (00:00 Astana)
        try:
            self.loop.create_task(self.birthday_loop())
        except Exception as e:
            logger.error(f"Failed to create birthday loop task: {e}", exc_info=True)
        
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
        await self.load_extension("cogs.games.coin_flip")
        await self.load_extension("cogs.games.dice_roll")
        await self.load_extension("cogs.games.guess_number")
        await self.load_extension("cogs.games.high_low")
        await self.load_extension("cogs.games.roulette")
        await self.load_extension("cogs.rob")
        await self.load_extension("cogs.bank")
        await self.load_extension("cogs.math_quiz")
        await self.load_extension("cogs.sell")
        await self.load_extension("cogs.thread")
        await self.load_extension("cogs.coin_drop")
        await self.load_extension("cogs.pet")
        await self.load_extension("cogs.isolate")
        await self.load_extension("cogs.br")
        await self.load_extension("cogs.mm")
        await self.load_extension("cogs.ultimatum")
        await self.load_extension("cogs.elo")
        await self.load_extension("cogs.team")
        await self.load_extension("cogs.bot_settings")
        await self.load_extension("cogs.moderation")
        await self.load_extension("cogs.freefire")
        await self.load_extension("cogs.statistic")
        await self.load_extension("cogs.ocr_upload")
        await self.load_extension("cogs.wardrobe")
        await self.load_extension("cogs.emoji")
        await self.load_extension("cogs.reputation")

        # Регистрация общих групп команд (/player, /economy, /games, ...)
        from cogs.groups import ROOT_GROUPS
        for _group in ROOT_GROUPS:
            try:
                self.tree.add_command(_group)
            except Exception as e:
                logger.warning("Не удалось зарегистрировать %s: %s", _group.name, e)

        # Components V2: author/аватар в командах + обёртки send/edit
        try:
            from utils.design import meta as _design_meta
            _design_meta.install()
            _design_meta.wrap_all_commands(self.tree)
        except Exception as e:
            logger.warning("Не удалось установить design-meta: %s", e)

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

            # MM-флоу: кнопка «Загрузить скриншот» живёт в карточке
            # матча (cs-4x4-match-N), а не в сообщении турнира —
            # восстанавливаем её persistent view для активных матчей.
            if getattr(tournament, "mm_search_channel_id", 0):
                try:
                    from services.match_announcer import _phase_matches
                    from views.ocr_match_view import (
                        MatchScreenshotButton,
                        _stats_complete,
                        match_is_finished,
                    )

                    for mt, mi, _a, _b in _phase_matches(tournament):
                        if match_is_finished(tournament, mt, mi):
                            continue
                        if _stats_complete(tournament, mt, mi):
                            continue
                        mm_view = discord.ui.View(timeout=None)
                        mm_view.add_item(
                            MatchScreenshotButton(
                                tournament.guild_id,
                                tournament.channel_id,
                                tournament,
                                mt, mi, "Загрузить скриншот",
                            )
                        )
                        self.add_view(mm_view)
                except Exception as exc:
                    logger.warning(
                        "MM match screenshot view restore failed for %s: %s",
                        tournament.id, exc,
                    )

            # Восстанавливаем кнопки публичных OCR-сообщений матчей
            for session in (tournament.ocr_sessions or {}).values():
                # Squad-режим: отдельные сообщения на каждый отряд
                if session.get("mode") == "squad":
                    try:
                        from views.ocr_match_view import (
                            SquadAssignedView,
                            SquadClaimView,
                        )

                        mt, mi = session["match_type"], session["match_index"]
                        for entry in session.get("squad_pool", []):
                            mid, uid = (
                                entry.get("message_id"), entry.get("uid")
                            )
                            if mid and uid:
                                self.add_view(
                                    SquadClaimView(
                                        tournament.guild_id,
                                        tournament.channel_id,
                                        tournament, mt, mi, uid,
                                    ),
                                    message_id=mid,
                                )
                        for tkey, sentry in session.get("squads", {}).items():
                            mid = sentry.get("message_id")
                            if not mid:
                                continue
                            try:
                                tidx = int(tkey)
                            except (TypeError, ValueError):
                                continue
                            self.add_view(
                                SquadAssignedView(
                                    tournament.guild_id,
                                    tournament.channel_id,
                                    tournament, mt, mi, tidx,
                                ),
                                message_id=mid,
                            )
                    except Exception as e:
                        logger.warning(
                            "Squad OCR view restore failed for %s: %s",
                            session.get("match_type"), e,
                        )
                    continue

                if (
                    session.get("status") in ("awaiting_mapping", "mapping", "ready")
                    and session.get("message_id")
                ):
                    try:
                        from views.ocr_match_view import OCRPublicView

                        ocr_view = OCRPublicView(
                            tournament.guild_id,
                            tournament.channel_id,
                            tournament,
                            session["match_type"],
                            session["match_index"],
                        )
                        self.add_view(
                            ocr_view, message_id=session["message_id"]
                        )
                    except Exception as e:
                        logger.warning(
                            "OCR view restore failed for %s: %s",
                            session.get("match_type"), e,
                        )

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
            return TeamsView(gid, tournament.channel_id, tournament)

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
            await message.edit(view=compose(embed, view))
        except discord.HTTPException as exc:
            logger.error("Не удалось обновить сообщение: %s", exc)
            return

        # Анонс старта матчей активной фазы в канал результатов
        try:
            from services.match_announcer import announce_phase_matches

            await announce_phase_matches(tournament, guild, self)
        except Exception as exc:
            logger.error("Match announce failed: %s", exc)

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
                                            new_message = await channel.send(f"⏱️ Время вышло! Случайный выбор: {player} был выбран.\n <@{next_captain_id}> - ваша очередь выбирать!")
                                        else:
                                            new_message = await channel.send(f"⏱️ Время вышло! Случайный выбор: {player} был выбран.\n {next_captain_name} - ваша очередь выбирать!")
                                        tournament.draft_message_id = new_message.id
                                        store.set(tournament)
                            else:
                                # Draft complete
                                logger.info(f"Draft completed automatically for guild {guild.id}")
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

    async def birthday_loop(self) -> None:
        """Поздравлять с днём рождения в 00:00 по времени Астаны (UTC+5)."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        self._last_birthday_day = None
        while not self.is_closed():
            try:
                await asyncio.sleep(30)
                if not self.is_ready():
                    continue

                now_astana = datetime.now(timezone.utc) + timedelta(hours=5)
                today = now_astana.date()
                # Срабатываем один раз в первый час после полуночи по Астане
                if now_astana.hour == 0 and self._last_birthday_day != today:
                    self._last_birthday_day = today
                    await self._send_birthday_greetings(today)
            except Exception as e:
                logger.error(f"Error in birthday loop: {e}", exc_info=True)

    async def _send_birthday_greetings(self, today) -> None:
        """Отправить поздравления всем именинникам за сегодня."""
        from config import NOTIFICATION_CHANNEL_ID
        from storage.player_stats_store import player_stats_store
        from views.birthdays_view import build_birthday_embed, days_until_birthday

        for guild in self.guilds:
            try:
                stats_list = await player_stats_store.get_all(guild.id)
                members = []
                for s in stats_list:
                    birthday = getattr(s, "birthday", "") or ""
                    if days_until_birthday(birthday, today) != 0:
                        continue
                    member = guild.get_member(s.user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(s.user_id)
                        except Exception:
                            member = None
                    if member is not None:
                        members.append(member)
                if not members:
                    continue

                channel = guild.get_channel(NOTIFICATION_CHANNEL_ID)
                if channel is None:
                    logger.warning(f"Birthday greetings: notification channel {NOTIFICATION_CHANNEL_ID} not found in guild {guild.id}")
                    continue

                for member in members:
                    try:
                        await channel.send(view=compose(build_birthday_embed(member)))
                    except Exception as e:
                        logger.error(f"Birthday greeting failed for {member.id}: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"Birthday greetings error for guild {guild.id}: {e}", exc_info=True)

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
