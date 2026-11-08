"""Cog для matchmaking системы."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING

import discord
from utils.design import compose, text_card
from discord import app_commands
from discord.ext import commands

from config import RANKED_READY_TIMEOUT_SECONDS, replace_emojis

# Единый канал для карточек «НАЙДЕН МАТЧ» (Ready Check)
MATCH_FOUND_CHANNEL_ID = 1568992622180311220
from models.mm_search import MMFormat, MMSearch
from models.player_stats import PlayerStats
from models.ready_check import ReadyCheck, ReadyCheckPlayer, ReadyCheckStatus
from models.tournament import FormationMode, RegistrationState, TournamentSize, TournamentPhase, Tournament
from storage.mm_search_store import mm_search_store
from storage.ready_check_store import ready_check_store
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from utils.embeds import build_setup_embed
from utils.match_found_renderer import (
    MatchFoundData,
    PlayerCard,
    TeamAverages,
    render_match_found,
)
from views.mm_search_view import build_search_embed, build_search_view
from views.ready_check_view import PNG_FILENAME, build_ready_embed, build_ready_view, ready_card

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Каналы для турниров
TOURNAMENT_CHANNEL_IDS = [1549809898643001484, 1563149967303118949, 1563149951033151609]

# Заблокированные каналы для поиска
LOCKED_CHANNELS: dict[int, str] = {}  # channel_id -> search_channel_id


# Профили статистики для /mm test: (elo, games, wins, kills, deaths, assists, damage)
_MM_TEST_PROFILES: list[tuple[int, int, int, int, int, int, int]] = [
    (2280, 140, 95, 1680, 420, 350, 455_000),   # топ: SR ~9.5, WR 68%, K/D 4.0
    (2050, 110, 66, 1180, 390, 300, 290_000),   # SR ~7.4, WR 60%, K/D 3.0
    (1820, 90, 47, 810, 360, 260, 180_000),     # SR ~5.5, WR 52%, K/D 2.3
    (1650, 75, 34, 560, 350, 220, 120_000),     # SR ~4.1, WR 45%, K/D 1.6
    (1490, 60, 24, 400, 330, 180, 78_000),      # SR ~3.2, WR 40%, K/D 1.2
    (1320, 45, 15, 270, 320, 140, 45_000),      # SR ~2.3, WR 33%, K/D 0.8
    (1180, 30, 8, 140, 290, 90, 21_000),        # SR ~1.4, WR 27%, K/D 0.5
    (1050, 15, 3, 55, 260, 50, 9_000),          # SR ~1.0, WR 20%, K/D 0.2
]


class MMCog(commands.Cog):
    """Cog с командами matchmaking."""

    def __init__(self, bot: TournamentBot):
        self.bot = bot
        self._search_timers: dict[tuple[int, int], asyncio.Task] = {}
        self._rc_locks: dict[str, asyncio.Lock] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._rc_sweeper_task: asyncio.Task | None = None
        self._rc_recovery_task: asyncio.Task | None = None
        self.bot.mm_cog = self  # Регистрация для доступа из view

    async def cog_load(self) -> None:
        """Запустить sweeper Ready Check и восстановление после рестарта."""
        self._rc_sweeper_task = asyncio.create_task(self._ready_check_sweeper())
        self._rc_recovery_task = asyncio.create_task(self._recover_ready_checks())

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
                # Отменить активный Ready Check этого поиска
                rc = await ready_check_store.get_active_by_search(
                    search.guild_id, search.channel_id
                )
                if rc is not None:
                    rc.status = ReadyCheckStatus.CANCELLED
                    rc.cancel_reason = "поиск удалён администратором"
                    await ready_check_store.save(rc)
                    await self._update_ready_message(rc)

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

    @mm_group.command(name="test", description="Тест «Найден матч» с искусственными игроками")
    @app_commands.describe(auto_ready="Автоматически подтверждать готовность")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_test(self, interaction: discord.Interaction, auto_ready: bool = True) -> None:
        """Запустить тест Ready Check: 8 искусственных игроков с разной статистикой."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Команда только на сервере.", ephemeral=True)
            return

        if mm_search_store.get(guild.id, interaction.channel_id):
            await interaction.response.send_message(
                "❌ В этом канале уже есть активный поиск.", ephemeral=True
            )
            return
        if await ready_check_store.get_active_by_search(guild.id, interaction.channel_id):
            await interaction.response.send_message(
                "❌ В этом канале уже идёт проверка готовности.", ephemeral=True
            )
            return

        # Свободный канал для турнира
        search = MMSearch(
            guild_id=guild.id,
            channel_id=interaction.channel_id,
            format=MMFormat.SQUAD,
        )
        for channel_id in TOURNAMENT_CHANNEL_IDS:
            if channel_id in LOCKED_CHANNELS:
                continue
            tournament_channel = self.bot.get_channel(channel_id)
            if tournament_channel is None:
                continue
            tournament = store.get(guild.id, tournament_channel.id)
            if not tournament or tournament.phase == TournamentPhase.COMPLETE:
                search.tournament_channel_id = channel_id
                LOCKED_CHANNELS[channel_id] = interaction.channel_id
                break
        if not search.tournament_channel_id:
            await interaction.response.send_message(
                "❌ Все каналы для турниров заняты.", ephemeral=True
            )
            return

        # Заполнить поиск искусственными игроками (отрицательные user_id, как в /tournament test)
        # Уникальная база на guild+channel+время, чтобы тесты не пересекались
        base_user_id = -(
            (guild.id % 100000) * 1_000_000
            + (interaction.channel_id % 1000) * 100
            + int(time.time()) % 100
        )
        test_names = [
            "TestAce", "TestHunter", "TestRusher", "TestSniper",
            "TestCamper", "TestRookie", "TestNoob", "TestGuest",
        ]
        for i, (name, profile) in enumerate(zip(test_names, _MM_TEST_PROFILES)):
            elo, games, wins, kills, deaths, assists, damage = profile
            user_id = base_user_id - i
            stats = PlayerStats(
                guild_id=guild.id,
                user_id=user_id,
                name=name,
                elo=elo,
                wins=wins,
                games=games,
                finals=min(wins // 4, games),
                tournament_participations=games,
                total_kills=kills,
                total_deaths=deaths,
                total_assists=assists,
                total_damage=damage,
                level=min(100, max(1, games // 2)),
            )
            await player_stats_store.set(stats)
            search.add_player(name, user_id)
        search.timer_started = True

        embed = await build_search_embed(search, guild)
        view = build_search_view(self.bot, search)
        message = await interaction.channel.send(view=compose(embed, view))
        search.message_id = message.id
        mm_search_store.set(search)

        rc = await self._start_ready_check(search, interaction)
        if rc is None:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Не удалось запустить проверку готовности.", ephemeral=True
                )
            return

        if auto_ready:
            task = asyncio.create_task(self._auto_ready_players(rc.match_id))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            await interaction.followup.send(
                "🧪 Тест запущен: 8 тестовых игроков, авто-готовность включена.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "🧪 Тест запущен: 8 тестовых игроков. Готовность не будет "
                "подтверждена — проверка завершится по таймауту.",
                ephemeral=True,
            )

    async def _auto_ready_players(self, match_id: str, delay: float = 0.8) -> None:
        """Тест: имитировать нажатие «Готов» за каждого участника матча."""
        try:
            rc = await ready_check_store.get(match_id)
            if rc is None:
                return
            for player in list(rc.players):
                rc = await ready_check_store.get(match_id)
                if rc is None or rc.status != ReadyCheckStatus.READY_CHECK:
                    return
                async with self._rc_lock(match_id):
                    await self._confirm_ready(match_id, player.user_id)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Auto-ready test failed for %s: %s", match_id, exc)

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
        message = await interaction.channel.send( view=compose(embed, view))

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
                        
                        await notification_channel.send( view=compose(embed, text=role_mention),
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
                            await message.edit( view=compose(embed, view))
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

    # ------------------------------------------------------------------
    # Ready Check («НАЙДЕН МАТЧ»)
    # ------------------------------------------------------------------

    def _rc_lock(self, match_id: str) -> asyncio.Lock:
        lock = self._rc_locks.get(match_id)
        if lock is None:
            lock = asyncio.Lock()
            self._rc_locks[match_id] = lock
        return lock

    async def _start_ready_check(self, search: MMSearch, interaction: discord.Interaction) -> ReadyCheck | None:
        """Запустить фазу Ready Check когда поиск заполнен (8 игроков)."""
        guild = interaction.guild
        if guild is None:
            return

        # Уже есть активная проверка для этого поиска — не дублировать
        existing = await ready_check_store.get_active_by_search(search.guild_id, search.channel_id)
        if existing is not None:
            logger.warning("Ready check already active for search %s/%s", search.guild_id, search.channel_id)
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        try:
            # SR + статистика для каждого игрока
            players_with_sr: list[tuple[str, float]] = []
            stats_map: dict[int, object] = {}
            for name in search.players:
                user_id = search.player_user_ids.get(name, 0)
                stats = await player_stats_store.get(guild.id, user_id) if user_id else None
                stats_map[user_id] = stats
                players_with_sr.append((name, stats.skill_rating if stats else 0.0))

            # Балансировка — тот же алгоритм, что и в _create_tournament_from_search
            probe = Tournament(
                guild_id=search.guild_id,
                channel_id=search.tournament_channel_id or interaction.channel_id,
                size=TournamentSize.EIGHT,
                formation_mode=FormationMode.RANDOM_AUTO,
            )
            teams = probe.distribute_players_random_auto(players_with_sr)
            if len(teams) != 2 or any(len(t) != 4 for t in teams):
                raise RuntimeError(f"Unexpected team split: {[len(t) for t in teams]}")

            # Карточки игроков для рендера
            members: dict[int, object] = {}
            for name in search.players:
                uid = search.player_user_ids.get(name, 0)
                if uid and uid not in members:
                    member = guild.get_member(uid)
                    if member is None:
                        try:
                            member = await guild.fetch_member(uid)
                        except Exception:
                            member = None
                    members[uid] = member

            def _card(name: str, team: str) -> PlayerCard:
                uid = search.player_user_ids.get(name, 0)
                stats = stats_map.get(uid)
                member = members.get(uid)
                avatar_url = ""
                if member is not None:
                    avatar_url = member.display_avatar.url
                    avatar_url += ("&" if "?" in avatar_url else "?") + "size=128"
                kd = 0.0
                winrate = 0.0
                if stats is not None:
                    kd = stats.total_kills / max(stats.total_deaths, 1)
                    winrate = stats.win_rate
                return PlayerCard(
                    user_id=uid,
                    name=name,
                    team=team,
                    elo=int(stats.elo) if stats else 0,
                    sr=float(stats.skill_rating) if stats else 0.0,
                    kd=kd,
                    winrate=winrate,
                    avatar_url=avatar_url,
                )

            cards_a = [_card(n, "A") for n in teams[0]]
            cards_b = [_card(n, "B") for n in teams[1]]

            # Средние по командам + вероятность победы (существующий алгоритм)
            from utils.rating_calculator import calculate_expected_win_probability

            def _avgs(cards: list[PlayerCard]) -> TeamAverages:
                return TeamAverages(
                    elo=sum(c.elo for c in cards) / len(cards),
                    sr=sum(c.sr for c in cards) / len(cards),
                    kd=sum(c.kd for c in cards) / len(cards),
                    win_chance=0.0,
                )

            avg_a, avg_b = _avgs(cards_a), _avgs(cards_b)
            p_a, p_b = calculate_expected_win_probability(avg_a.sr, avg_b.sr)
            avg_a.win_chance = p_a * 100
            avg_b.win_chance = p_b * 100

            render_data = MatchFoundData(
                match_num=int(time.time()) % 10000,
                team_a=cards_a,
                team_b=cards_b,
                avg_a=avg_a,
                avg_b=avg_b,
            )

            rc_players = [
                ReadyCheckPlayer(user_id=c.user_id, name=c.name, team=c.team)
                for c in cards_a + cards_b
            ]
            match_id = f"rc_{search.guild_id}_{int(time.time() * 1000)}"
            rc = ReadyCheck(
                match_id=match_id,
                guild_id=search.guild_id,
                search_channel_id=search.channel_id,
                tournament_channel_id=search.tournament_channel_id,
                players=rc_players,
                deadline=time.time() + RANKED_READY_TIMEOUT_SECONDS,
                match_num=render_data.match_num,
            )
            if not await ready_check_store.create(rc):
                await interaction.followup.send("❌ Проверка готовности уже запущена.", ephemeral=True)
                return

            # Рендер PNG (аватарки + Pillow — в thread, не блокирует loop)
            png = await render_match_found(render_data)

            file = discord.File(io.BytesIO(png), filename=PNG_FILENAME)
            # Карточка «Найден матч» — всегда в фиксированный канал
            card_channel = self.bot.get_channel(MATCH_FOUND_CHANNEL_ID)
            if card_channel is None:
                try:
                    card_channel = await self.bot.fetch_channel(MATCH_FOUND_CHANNEL_ID)
                except Exception:
                    card_channel = None
            if card_channel is None:
                card_channel = interaction.channel
            message = await card_channel.send(
                view=ready_card(rc, self.bot),
                file=file,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            rc.channel_id = card_channel.id
            rc.message_id = message.id
            await ready_check_store.save(rc)

            # Persistent view для восстановления после рестарта
            try:
                self.bot.add_view(build_ready_view(self.bot, rc.match_id), message_id=message.id)
            except Exception as e:
                logger.warning("add_view for ready check failed: %s", e)

            await interaction.followup.send(
                "🏆 Матч найден! Подтвердите готовность в течение 90 секунд.",
                ephemeral=True,
            )
            logger.info("Ready check %s started (%d players)", match_id, len(rc_players))
            return rc

        except Exception as exc:
            logger.error("Failed to start ready check: %s", exc, exc_info=True)
            try:
                await interaction.followup.send("❌ Ошибка при создании проверки готовности.", ephemeral=True)
            except Exception:
                pass

    async def _update_ready_message(self, rc: ReadyCheck) -> None:
        """Обновить основное сообщение Ready Check (без спама)."""
        try:
            channel = self.bot.get_channel(rc.channel_id) or await self.bot.fetch_channel(rc.channel_id)
            if channel is None:
                return
            message = await channel.fetch_message(rc.message_id)
            disabled = rc.status != ReadyCheckStatus.READY_CHECK
            await message.edit(view=ready_card(rc, self.bot, disabled=disabled))
        except discord.NotFound:
            logger.warning("Ready check message %s not found", rc.message_id)
        except Exception as exc:
            logger.error("Failed to update ready check message: %s", exc)

    async def _confirm_ready(self, match_id: str, user_id: int) -> str:
        """Отметить игрока готовым и при 8/8 запустить матч. Возвращает текст ответа.

        Вызывать под self._rc_lock(match_id). Общий путь для кнопки и автотеста.
        """
        rc = await ready_check_store.get(match_id)
        if rc is None:
            return "❌ Матч не найден."
        player = rc.player_by_id(user_id)
        if player is None:
            return "❌ Вы не участвуете в этом матче."
        if rc.status != ReadyCheckStatus.READY_CHECK:
            return "ℹ️ Этот матч уже завершён."
        if time.time() > rc.deadline:
            return "⏰ Время подтверждения истекло."
        if player.is_ready:
            return f"✅ Вы уже подтвердили готовность ({rc.ready_count}/8)."

        player.is_ready = True
        player.ready_at = time.time()
        await ready_check_store.save(rc)
        await self._update_ready_message(rc)

        if rc.ready_count >= len(rc.players):
            # Атомарный переход READY_CHECK -> ALL_READY -> LAUNCHING
            if await ready_check_store.try_transition(
                match_id, ReadyCheckStatus.READY_CHECK, ReadyCheckStatus.ALL_READY
            ):
                rc = await ready_check_store.get(match_id)
                await self._update_ready_message(rc)
                rc.status = ReadyCheckStatus.LAUNCHING
                await ready_check_store.save(rc)
                await self._launch_ready_check(rc)
                return "✅ Все игроки готовы — матч запускается."
        return f"✅ Готовность подтверждена. Ожидаем остальных игроков ({rc.ready_count}/8)."

    async def handle_ready_click(self, interaction: discord.Interaction, match_id: str) -> None:
        """Кнопка «Готов»."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        async with self._rc_lock(match_id):
            text = await self._confirm_ready(match_id, interaction.user.id)
        try:
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            pass

    async def handle_decline_click(self, interaction: discord.Interaction, match_id: str) -> None:
        """Кнопка «Отказаться»."""
        rc = await ready_check_store.get(match_id)
        if rc is None:
            await interaction.response.send_message("❌ Матч не найден.", ephemeral=True)
            return
        player = rc.player_by_id(interaction.user.id)
        if player is None:
            await interaction.response.send_message("❌ Вы не участвуете в этом матче.", ephemeral=True)
            return

        async with self._rc_lock(match_id):
            rc = await ready_check_store.get(match_id)
            if rc is None:
                await interaction.response.send_message("❌ Матч не найден.", ephemeral=True)
                return
            player = rc.player_by_id(interaction.user.id)
            if rc.status != ReadyCheckStatus.READY_CHECK:
                await interaction.response.send_message("ℹ️ Этот матч уже завершён.", ephemeral=True)
                return

            player.declined = True
            rc.status = ReadyCheckStatus.DECLINED
            rc.cancel_reason = f"{player.name} отказался от участия"
            await ready_check_store.save(rc)

            await interaction.response.send_message(
                "❌ Вы отказались от матча. Остальные игроки возвращены в очередь.", ephemeral=True
            )
            await self._update_ready_message(rc)
            await self._finish_cancelled(rc, keep_user_ids={p.user_id for p in rc.players if p.user_id != player.user_id})

    async def _expire_ready_check(self, match_id: str) -> None:
        """Таймаут Ready Check: TIMEOUT -> отмена -> реqueue готовых."""
        async with self._rc_lock(match_id):
            rc = await ready_check_store.get(match_id)
            if rc is None or rc.status != ReadyCheckStatus.READY_CHECK:
                return
            # Все 8 успели нажать, но запуск не завершился — запускаем
            if rc.ready_count >= len(rc.players):
                if await ready_check_store.try_transition(
                    match_id, ReadyCheckStatus.READY_CHECK, ReadyCheckStatus.ALL_READY
                ):
                    rc = await ready_check_store.get(match_id)
                    rc.status = ReadyCheckStatus.LAUNCHING
                    await ready_check_store.save(rc)
                    await self._launch_ready_check(rc)
                return

            rc.status = ReadyCheckStatus.TIMEOUT
            rc.cancel_reason = f"подтверждение не завершено ({rc.ready_count}/8)"
            await ready_check_store.save(rc)
            await self._update_ready_message(rc)
            keep = {p.user_id for p in rc.players if p.is_ready}
            await self._finish_cancelled(rc, keep_user_ids=keep)

    async def _finish_cancelled(self, rc: ReadyCheck, keep_user_ids: set[int]) -> None:
        """Вернуть игроков в очередь после отмены и обновить search-embed."""
        await self._requeue_players(rc, keep_user_ids)

    async def _requeue_players(self, rc: ReadyCheck, keep_user_ids: set[int]) -> None:
        """Вернуть подмножество игроков в MM-поиск, сохранив порядок очереди."""
        search = mm_search_store.get(rc.guild_id, rc.search_channel_id)
        if search is None:
            logger.warning("Search %s/%s not found for requeue", rc.guild_id, rc.search_channel_id)
            return

        keep_names = [
            name for name in search.players
            if search.player_user_ids.get(name, 0) in keep_user_ids
        ]
        search.players = keep_names
        search.player_user_ids = {n: search.player_user_ids[n] for n in keep_names if n in search.player_user_ids}
        search.timer_started = bool(keep_names)
        mm_search_store.set(search)

        # Перезапустить таймер поиска, если игроки вернулись
        if keep_names:
            key = (search.guild_id, search.channel_id)
            if key not in self._search_timers or self._search_timers[key].done():
                self._start_search_timer(search.guild_id, search.channel_id)
        else:
            self._cancel_search_timer(search.guild_id, search.channel_id)

        # Обновить embed поиска
        try:
            channel = self.bot.get_channel(search.channel_id)
            if channel and search.message_id:
                message = await channel.fetch_message(search.message_id)
                embed = await build_search_embed(search, channel.guild)
                view = build_search_view(self.bot, search)
                await message.edit(view=compose(embed, view))
        except Exception as e:
            logger.error("Error updating search embed after ready-check cancel: %s", e)

    async def _launch_ready_check(self, rc: ReadyCheck) -> None:
        """8/8 — запустить существующий турнирный флоу ровно один раз."""
        guild_id = rc.guild_id
        try:
            # Дедупликация: турнир уже мог быть создан (retry/recovery)
            for t in store.get_by_guild(guild_id):
                if (
                    getattr(t, "mm_search_channel_id", 0) == rc.search_channel_id
                    and t.phase != TournamentPhase.COMPLETE
                ):
                    logger.info("Tournament already exists for search channel %s — skip launch", rc.search_channel_id)
                    rc.status = ReadyCheckStatus.LAUNCHED
                    await ready_check_store.save(rc)
                    await self._update_ready_message(rc)
                    return

            search = mm_search_store.get(guild_id, rc.search_channel_id)
            if search is None or not search.is_full:
                logger.error("Cannot launch ready check %s: search missing or not full", rc.match_id)
                rc.status = ReadyCheckStatus.CANCELLED
                rc.cancel_reason = "поиск был изменён до запуска"
                await ready_check_store.save(rc)
                await self._update_ready_message(rc)
                return

            channel = self.bot.get_channel(rc.search_channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(rc.search_channel_id)
                except Exception:
                    channel = None
            if channel is None:
                raise RuntimeError("search channel not found")

            await self._create_tournament_from_search(search, channel)

            rc.status = ReadyCheckStatus.LAUNCHED
            await ready_check_store.save(rc)
            await self._update_ready_message(rc)
            self._rc_locks.pop(rc.match_id, None)
            logger.info("Ready check %s launched tournament", rc.match_id)
        except Exception as exc:
            logger.error("Failed to launch tournament from ready check %s: %s", rc.match_id, exc, exc_info=True)
            rc.status = ReadyCheckStatus.CANCELLED
            rc.cancel_reason = "внутренняя ошибка запуска"
            await ready_check_store.save(rc)
            await self._update_ready_message(rc)
            # Вернуть всех в очередь — матч не состоялся
            await self._requeue_players(rc, {p.user_id for p in rc.players})

    async def _ready_check_sweeper(self) -> None:
        """Фоновая задача: истёкшие deadline и застрявшие ALL_READY."""
        while True:
            try:
                await asyncio.sleep(10)
                for rc in await ready_check_store.list_active():
                    try:
                        if rc.status == ReadyCheckStatus.READY_CHECK and time.time() > rc.deadline:
                            await self._expire_ready_check(rc.match_id)
                        elif rc.status in (
                            ReadyCheckStatus.ALL_READY,
                            ReadyCheckStatus.LAUNCHING,
                        ):
                            # Восстановление запуска после рестарта/сбоя
                            async with self._rc_lock(rc.match_id):
                                fresh = await ready_check_store.get(rc.match_id)
                                if fresh and fresh.status in (
                                    ReadyCheckStatus.ALL_READY,
                                    ReadyCheckStatus.LAUNCHING,
                                ):
                                    await self._launch_ready_check(fresh)
                    except Exception as exc:
                        logger.error("Sweeper error for %s: %s", rc.match_id, exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Ready check sweeper error: %s", exc)

    async def _recover_ready_checks(self) -> None:
        """Восстановление после рестарта: persistent views + дедлайны."""
        try:
            await asyncio.sleep(8)  # дать кэшам guild/channel прогрузиться
            actives = await ready_check_store.list_active()
            for rc in actives:
                try:
                    disabled = rc.status != ReadyCheckStatus.READY_CHECK
                    self.bot.add_view(
                        build_ready_view(self.bot, rc.match_id, disabled=disabled)
                    )
                    if rc.status == ReadyCheckStatus.READY_CHECK:
                        if time.time() > rc.deadline:
                            await self._expire_ready_check(rc.match_id)
                        else:
                            # Подтянуть сообщение до актуального состояния
                            await self._update_ready_message(rc)
                    elif rc.status in (
                        ReadyCheckStatus.ALL_READY,
                        ReadyCheckStatus.LAUNCHING,
                    ):
                        async with self._rc_lock(rc.match_id):
                            fresh = await ready_check_store.get(rc.match_id)
                            if fresh and fresh.status in (
                                ReadyCheckStatus.ALL_READY,
                                ReadyCheckStatus.LAUNCHING,
                            ):
                                await self._launch_ready_check(fresh)
                except Exception as exc:
                    logger.error("Recovery error for ready check %s: %s", rc.match_id, exc)
            if actives:
                logger.info("Recovered %d ready checks", len(actives))
        except Exception as exc:
            logger.error("Ready check recovery failed: %s", exc)

    async def _create_tournament_from_search(self, search: MMSearch, interaction_channel: discord.TextChannel) -> None:
        """Создать обычный турнир из поиска."""
        guild = interaction_channel.guild or self.bot.get_guild(search.guild_id)
        
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
        message = await tournament_channel.send( view=compose(embed, view))
        tournament.message_id = message.id
        store.set(tournament)

        # Анонс старта матчей в канал результатов (MM-флоу не проходит
        # через update_tournament_message)
        try:
            from services.match_announcer import announce_phase_matches

            await announce_phase_matches(tournament, guild)
        except Exception as exc:
            logger.error("Match announce failed: %s", exc)

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
            await search_message.edit( view=compose(search_embed, search_view))
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
                                await message.edit( view=compose(embed, view))
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
