"""Cog для matchmaking системы."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from utils.design import compose, text_card
from discord import app_commands
from discord.ext import commands

from config import (
    RANKED_READY_TIMEOUT_SECONDS, replace_emojis, ORG_ROLE_ID,
    ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID, MM_MATCH_CHANNEL_POOL,
)

# Единый канал для карточек «НАЙДЕН МАТЧ» (Ready Check)
MATCH_FOUND_CHANNEL_ID = 1549809898643001484

# VAC 30 минут — не нажал «Готов» / нажал «Отказаться» на Ready Check
MM_NO_SHOW_VAC = timedelta(minutes=30)
# Пауза между попытками запуска матча после ошибки (антиспам API)
MATCH_START_RETRY_COOLDOWN = 120.0
from models.mm_search import MMFormat, MMSearch, QUEUE_OPEN, QUEUE_LOCKED_READY_CHECK
from models.player_stats import PlayerStats
from models.ready_check import ReadyCheck, ReadyCheckPlayer, ReadyCheckStatus
from models.tournament import FormationMode, RegistrationState, TournamentSize, TournamentPhase, Tournament
from storage.mm_search_store import mm_search_store
from storage.ready_check_store import ready_check_store
from storage.match_number_store import match_number_store
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from utils.embeds import build_setup_embed
from utils.match_found_renderer import (
    MatchFoundData,
    PlayerCard,
    TeamAverages,
    render_match_found,
)
from views.mm_search_view import (
    build_queue_controls,
    build_queue_panel,
)
from views.ready_check_view import PNG_FILENAME, build_ready_embed, build_ready_view, ready_card

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Каналы для турниров
TOURNAMENT_CHANNEL_IDS = [1549809898643001484, 1563149967303118949, 1563149951033151609]

# Заблокированные каналы для поиска
LOCKED_CHANNELS: dict[int, str] = {}  # channel_id -> search_channel_id

# Баннер панели очереди (MediaGallery attachment://)
QUEUE_BANNER_PATH = Path(__file__).resolve().parent.parent / "assets" / "queue_banner.png"


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
        self._queue_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._begin_fail_at: dict[tuple[int, int], float] = {}
        self.bot.mm_cog = self  # Регистрация для доступа из view

    async def cog_load(self) -> None:
        """Загрузить очереди, запустить sweeper Ready Check и восстановление."""
        try:
            await mm_search_store.load()
        except Exception as exc:
            logger.error("MMSearchStore load failed: %s", exc)
        self._rc_sweeper_task = asyncio.create_task(self._ready_check_sweeper())
        self._rc_recovery_task = asyncio.create_task(self._recover_ready_checks())
        self._queue_recovery_task = asyncio.create_task(self._recover_queues())

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
                    await self._delete_match_channel(rc)

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

        # Не создаём новый поиск — заполняем уже активный:
        # сначала в этом канале, иначе любой активный поиск сервера.
        search = mm_search_store.get(guild.id, interaction.channel_id)
        if search is None:
            searches = mm_search_store.get_by_guild(guild.id)
            search = searches[0] if searches else None
        if search is None:
            await interaction.response.send_message(
                "❌ Нет активного поиска — /mmtest только дозаполняет "
                "существующий поиск.", ephemeral=True
            )
            return
        if await ready_check_store.get_active_by_search(
            guild.id, search.channel_id
        ):
            await interaction.response.send_message(
                "❌ В канале этого поиска уже идёт проверка готовности.",
                ephemeral=True
            )
            return

        needed = search.required_players - len(search.players)
        if needed <= 0:
            await interaction.response.send_message(
                "✅ Поиск уже заполнен.", ephemeral=True
            )
            return

        # Уникальная база на guild+channel+время, чтобы тесты не пересекались
        base_user_id = -(
            (guild.id % 100000) * 1_000_000
            + (search.channel_id % 1000) * 100
            + int(time.time()) % 100
        )
        # Имена из тестового пула, пропуская уже занятые в поиске
        name_pool = [n for n in (
            "TestAce", "TestHunter", "TestRusher", "TestSniper",
            "TestCamper", "TestRookie", "TestNoob", "TestGuest",
        ) if n not in search.players]
        added = 0
        for i in range(needed):
            name = name_pool[i] if i < len(name_pool) else f"TestBot{added + 1}"
            while name in search.players:
                added += 1
                name = f"TestBot{added + 1}"
            elo, games, wins, kills, deaths, assists, damage = (
                _MM_TEST_PROFILES[i % len(_MM_TEST_PROFILES)]
            )
            user_id = base_user_id - added - 1
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
            if search.add_player(name, user_id):
                added += 1

        # Таймер поиска — как при обычном входе первого игрока
        if not search.timer_started:
            search.timer_started = True
            self._start_search_timer(search.guild_id, search.channel_id)
        mm_search_store.set(search)

        # Обновить панель очереди
        try:
            await self._update_queue_panel(search)
        except Exception as exc:
            logger.error("mmtest: failed to refresh queue panel: %s", exc)

        # Поиск заполнен — блокируем очередь, создаём канал и Ready Check
        if search.is_full:
            self._cancel_search_timer(search.guild_id, search.channel_id)
            rc, err = await self._begin_match(search, interaction)
            if rc is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        err or "❌ Не удалось запустить проверку готовности.",
                        ephemeral=True,
                    )
                return
            if auto_ready:
                task = asyncio.create_task(
                    self._auto_ready_players(rc.match_id)
                )
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
                await interaction.followup.send(
                    "🧪 Тест: поиск дозаполнен до 8, авто-готовность включена.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "🧪 Тест: поиск дозаполнен до 8. Готовность не будет "
                    "подтверждена — проверка завершится по таймауту.",
                    ephemeral=True,
                )
            return

        await interaction.response.send_message(
            f"🧪 Добавлено {added} тестовых игроков — "
            f"в поиске {len(search.players)}/{search.required_players}.",
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
                # Авто-готовность — только для тестовых игроков
                # (отрицательные ID из /mmtest). Реальные участники
                # подтверждают готовность кнопкой самостоятельно.
                if player.user_id and player.user_id > 0:
                    continue
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
        await interaction.response.send_message("Поиск создан!", ephemeral=True)

        # Вечная V2-панель очереди с баннером
        banner_file = (
            discord.File(QUEUE_BANNER_PATH, filename="queue_banner.png")
            if QUEUE_BANNER_PATH.exists()
            else None
        )
        message = await interaction.channel.send(
            view=build_queue_panel(search),
            file=banner_file,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        search.message_id = message.id
        mm_search_store.set(search)

        # Persistent view для кнопок панели (восстановление после рестарта)
        try:
            self.bot.add_view(
                build_queue_controls(search.guild_id, search.channel_id),
                message_id=message.id,
            )
        except Exception as e:
            logger.warning("add_view for queue panel failed: %s", e)

        logger.info("Создан поиск %s в канале %s", format.value, interaction.channel_id)
        # Таймер НЕ запускаем при создании — только когда в поиске
        # появится хотя бы один игрок (handle_queue_join).

    async def _notify_search_started(self, guild_id: int, channel_id: int) -> None:
        """Embed-уведомление о поиске в канал уведомлений с пингом роли."""
        try:
            from config import NOTIFICATION_CHANNEL_ID, NOTIFICATION_ROLE_ID
            notification_channel = self.bot.get_channel(NOTIFICATION_CHANNEL_ID)
            if notification_channel is None:
                try:
                    notification_channel = await self.bot.fetch_channel(
                        NOTIFICATION_CHANNEL_ID)
                except Exception as e:
                    logger.warning(
                        "MM notify: fetch notification channel failed: %s", e)
            search_channel = self.bot.get_channel(channel_id)
            if search_channel is None:
                try:
                    search_channel = await self.bot.fetch_channel(channel_id)
                except Exception as e:
                    logger.warning("MM notify: fetch search channel failed: %s", e)
            if not notification_channel or not search_channel:
                logger.warning(
                    "MM notify skipped: notify_ch=%s search_ch=%s",
                    notification_channel, search_channel)
                return
            guild_obj = self.bot.get_guild(guild_id)
            role = guild_obj.get_role(NOTIFICATION_ROLE_ID) if guild_obj else None
            role_mention = role.mention if role else ""

            embed = discord.Embed(
                title="🔍 Поиск игроков",
                description=f"{search_channel.mention}",
                color=discord.Color.from_rgb(69, 69, 69)
            )
            await notification_channel.send(
                content=role_mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True)
            )
        except Exception as e:
            logger.error(f"Error sending search notification: {e}")

    def _start_search_timer(self, guild_id: int, channel_id: int) -> None:
        """Таймер поиска 10 минут: пинги на 30с / 3мин / 7мин, кик в конце."""
        async def timer_task():
            try:
                # Уведомления: 30 сек, 3 мин, 7 мин после первого игрока
                for delay in (30, 150, 240):
                    await asyncio.sleep(delay)
                    search = mm_search_store.get(guild_id, channel_id)
                    if not search or not search.players:
                        return
                    await self._notify_search_started(guild_id, channel_id)

                # 10 минут — кик, если поиск не заполнен
                await asyncio.sleep(180)

                search = mm_search_store.get(guild_id, channel_id)
                if not search:
                    return

                # Если не заполнен, кикнуть игроков и сбросить канал
                if not search.is_full:
                    # Разблокировать канал
                    if search.tournament_channel_id in LOCKED_CHANNELS:
                        del LOCKED_CHANNELS[search.tournament_channel_id]

                    # Кикнуть игроков но сохранить поиск активным
                    search.players = []
                    search.player_user_ids = {}
                    search.timer_started = False
                    search.tournament_channel_id = 0
                    mm_search_store.set(search)

                    # Обновить панель (без сообщения в канал)
                    try:
                        await self._update_queue_panel(search)
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

    async def _start_ready_check(
        self,
        search: MMSearch,
        interaction: discord.Interaction,
        match_id: str | None = None,
        match_num: int = 0,
        match_channel_id: int = 0,
    ) -> ReadyCheck | None:
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
                match_num=match_num or int(time.time()) % 10000,
                team_a=cards_a,
                team_b=cards_b,
                avg_a=avg_a,
                avg_b=avg_b,
            )

            rc_players = [
                ReadyCheckPlayer(user_id=c.user_id, name=c.name, team=c.team)
                for c in cards_a + cards_b
            ]
            if not match_id:
                match_id = f"rc_{search.guild_id}_{int(time.time() * 1000)}"
            rc = ReadyCheck(
                match_id=match_id,
                guild_id=search.guild_id,
                search_channel_id=search.channel_id,
                tournament_channel_id=search.tournament_channel_id,
                players=rc_players,
                deadline=time.time() + RANKED_READY_TIMEOUT_SECONDS,
                match_num=render_data.match_num,
                match_channel_id=match_channel_id,
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

            # Обновить и панель очереди — живой счётчик готовности
            try:
                search = mm_search_store.get(rc.guild_id, rc.search_channel_id)
                if search is not None and search.is_locked:
                    await self._update_queue_panel(search)
            except Exception:
                pass
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

    async def _issue_mm_vacs(
        self, guild_id: int, user_ids, reason: str
    ) -> None:
        """VAC 30 мин игрокам, не подтвердившим готовность / отказавшимся."""
        from services.vac_service import issue_vac
        issued_by = getattr(getattr(self.bot, "user", None), "id", 0) or 0
        for uid in {int(u) for u in user_ids if u and int(u) > 0}:
            try:
                row, err = await issue_vac(
                    guild_id, uid, issued_by, MM_NO_SHOW_VAC)
                if row is None:
                    continue  # already_active или ошибка — не дублируем
                logger.info("MM VAC issued: user=%s reason=%s", uid, reason)
                try:
                    user = self.bot.get_user(uid)
                    if user is None:
                        user = await self.bot.fetch_user(uid)
                    expires = row.get("expires_at")
                    ts = int(expires.timestamp()) if hasattr(
                        expires, "timestamp") else 0
                    await user.send(
                        f"Вам выдан VAC на 30 минут: {reason}.\n"
                        f"До <t:{ts}:R> вы не можете участвовать "
                        "в турнирах r!z3."
                    )
                except Exception:
                    pass  # DM закрыты — VAC всё равно выдан
            except Exception:
                logger.exception("Failed to issue MM VAC for %s", uid)

    async def handle_decline_click(self, interaction: discord.Interaction, match_id: str) -> None:
        """Кнопка «Отказаться»."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        rc = await ready_check_store.get(match_id)
        if rc is None:
            await self._ephemeral(interaction, "❌ Матч не найден.")
            return
        player = rc.player_by_id(interaction.user.id)
        if player is None:
            await self._ephemeral(interaction, "❌ Вы не участвуете в этом матче.")
            return

        async with self._rc_lock(match_id):
            rc = await ready_check_store.get(match_id)
            if rc is None:
                await self._ephemeral(interaction, "❌ Матч не найден.")
                return
            player = rc.player_by_id(interaction.user.id)
            if rc.status != ReadyCheckStatus.READY_CHECK:
                await self._ephemeral(interaction, "ℹ️ Этот матч уже завершён.")
                return

            player.declined = True
            rc.status = ReadyCheckStatus.DECLINED
            rc.cancel_reason = f"{player.name} отказался от участия"
            await ready_check_store.save(rc)
            await self._issue_mm_vacs(
                rc.guild_id, [player.user_id],
                "отказ от Ranked-матча")

            await self._ephemeral(
                interaction,
                "❌ Вы отказались от матча. Остальные игроки возвращены в очередь.",
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
            no_show = [p.user_id for p in rc.players if not p.is_ready]
            if no_show:
                await self._issue_mm_vacs(
                    rc.guild_id, no_show,
                    "не подтвердил готовность на матч")
            await self._finish_cancelled(rc, keep_user_ids=keep)

    async def _finish_cancelled(self, rc: ReadyCheck, keep_user_ids: set[int]) -> None:
        """Вернуть игроков в очередь после отмены, удалить канал матча."""
        await self._requeue_players(rc, keep_user_ids)
        await self._delete_match_channel(rc)

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
        # Очередь снова открыта — готовые игроки ждут следующего матча
        search.state = QUEUE_OPEN
        search.match_num = 0
        mm_search_store.set(search)

        # Перезапустить таймер поиска, если игроки вернулись
        if keep_names:
            key = (search.guild_id, search.channel_id)
            if key not in self._search_timers or self._search_timers[key].done():
                self._start_search_timer(search.guild_id, search.channel_id)
        else:
            self._cancel_search_timer(search.guild_id, search.channel_id)

        # Обновить панель очереди
        try:
            await self._update_queue_panel(search)
        except Exception as e:
            logger.error("Error updating queue panel after ready-check cancel: %s", e)

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
                    # Догнать анонс матчей, если он не успел выполниться
                    try:
                        guild = self.bot.get_guild(guild_id)
                        if guild is not None:
                            from services.match_announcer import announce_phase_matches
                            await announce_phase_matches(t, guild, self.bot)
                    except Exception as exc:
                        logger.warning("Announce catch-up failed: %s", exc)
                    return

            search = mm_search_store.get(guild_id, rc.search_channel_id)
            if search is None or not search.is_full:
                logger.error("Cannot launch ready check %s: search missing or not full", rc.match_id)
                rc.status = ReadyCheckStatus.CANCELLED
                rc.cancel_reason = "поиск был изменён до запуска"
                await ready_check_store.save(rc)
                await self._update_ready_message(rc)
                await self._finish_cancelled(rc, {p.user_id for p in rc.players})
                return

            # Прокинуть в турнирный флоу номер/канал/PNG карточки матча
            search.mm_match_num = rc.match_num
            search.mm_match_channel_id = rc.match_channel_id
            search.mm_card_png = await self._fetch_match_card_png(rc)

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
            await self._finish_cancelled(rc, {p.user_id for p in rc.players})

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
                # Self-heal: очередь в locked без живого Ready Check —
                # снимаем блокировку и обновляем панель
                try:
                    for s in mm_search_store.all():
                        if not s.is_locked:
                            continue
                        rc_active = (
                            await ready_check_store.get_active_by_search(
                                s.guild_id, s.channel_id
                            )
                        )
                        if rc_active is not None:
                            continue
                        s.state = QUEUE_OPEN
                        s.match_num = 0
                        s.timer_started = bool(s.players)
                        mm_search_store.set(s)
                        await self._update_queue_panel(s)
                        logger.warning(
                            "Stuck MM queue unlocked: %s/%s",
                            s.guild_id, s.channel_id,
                        )
                except Exception as exc:
                    logger.error("Queue unlock sweeper error: %s", exc)
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
        
        # Канал турнира — созданный ботом канал матча cs-4x4-match-N;
        # фиксированные каналы турниров больше не резервируются
        tournament_channel_id = int(getattr(search, "mm_match_channel_id", 0) or 0)
        tournament_channel = None
        if tournament_channel_id:
            tournament_channel = self.bot.get_channel(tournament_channel_id)
            if tournament_channel is None:
                try:
                    tournament_channel = await self.bot.fetch_channel(tournament_channel_id)
                except Exception:
                    tournament_channel = None
        if tournament_channel is None:
            tournament_channel = interaction_channel

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

        # MM: турнирный embed (сетка + кнопка комнаты) в канал матча
        # не отправляем — там только карточка «Матч начался»,
        # кнопка комнаты перенесена в неё (RoomButton).
        tournament.message_id = 0
        store.set(tournament)

        # Привязать заранее созданный канал матча и PNG карточки
        # (MM-флоу: канал cs-4x4-match-N создаётся на этапе ready-check)
        mm_channel_id = int(getattr(search, "mm_match_channel_id", 0) or 0)
        if mm_channel_id:
            tournament.mm_preset_channel_id = mm_channel_id
        mm_png = getattr(search, "mm_card_png", None)
        if mm_png:
            tournament.mm_card_png = mm_png

        # Тот же номер матча, что на карточке «НАЙДЕН МАТЧ» и в имени канала
        mm_num = int(getattr(search, "mm_match_num", 0) or 0)
        if mm_num:
            try:
                from services.match_announcer import _match_key, _phase_matches
                from storage.match_number_store import match_number_store as _mns

                for mt, mi, _a, _b in _phase_matches(tournament):
                    await _mns.assign(_match_key(tournament, mt, mi), mm_num)
            except Exception as exc:
                logger.warning("match number pre-assign failed: %s", exc)

        # Анонс старта матчей в канал результатов (MM-флоу не проходит
        # через update_tournament_message)
        try:
            from services.match_announcer import announce_phase_matches

            await announce_phase_matches(tournament, guild, self.bot)
        except Exception as exc:
            logger.error("Match announce failed: %s", exc)

        logger.info("Создан турнир из поиска %s в канале %s", search.format.value, tournament_channel.id)

        # Разблокировать канал после создания турнира
        if tournament_channel_id in LOCKED_CHANNELS:
            del LOCKED_CHANNELS[tournament_channel_id]

        # Сбросить поиск — очередь снова открыта для следующего матча
        search.clear()
        search.state = QUEUE_OPEN
        search.match_num = 0
        for attr in ("mm_match_num", "mm_match_channel_id", "mm_card_png"):
            try:
                delattr(search, attr)
            except AttributeError:
                pass
        mm_search_store.set(search)

        # Обновить панель очереди
        try:
            await self._update_queue_panel(search)
        except Exception as e:
            logger.error(f"Error updating queue panel: {e}")

    # ------------------------------------------------------------------
    # Очередь: join/leave, блокировка, канал матча
    # ------------------------------------------------------------------

    def _queue_lock(self, guild_id: int, channel_id: int) -> asyncio.Lock:
        """Per-queue lock — сериализует join/leave/begin_match."""
        key = (guild_id, channel_id)
        lock = self._queue_locks.get(key)
        if lock is None:
            lock = self._queue_locks[key] = asyncio.Lock()
        return lock

    async def _update_queue_panel(self, search: MMSearch) -> None:
        """Обновить V2-панель очереди на её сообщении."""
        if not search.message_id:
            return
        channel = self.bot.get_channel(search.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(search.channel_id)
            except Exception:
                return
        # Счётчик готовности для locked-состояния панели
        ready_count = None
        if search.is_locked:
            try:
                rc = await ready_check_store.get_active_by_search(
                    search.guild_id, search.channel_id
                )
                if rc is not None:
                    ready_count = rc.ready_count
            except Exception:
                pass
        try:
            message = await channel.fetch_message(search.message_id)
            await message.edit(
                view=build_queue_panel(search, ready_count=ready_count),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            logger.warning("Queue panel message %s not found", search.message_id)
        except Exception as exc:
            logger.error("Failed to update queue panel: %s", exc)

    async def _validate_join(
        self, interaction: discord.Interaction, search: MMSearch
    ) -> str | None:
        """Проверки перед входом в очередь. Возвращает текст ошибки или None."""
        guild_id = search.guild_id

        # Активный турнир
        for tournament in store.get_by_guild(guild_id):
            if tournament.is_player_active(interaction.user.id):
                return ("Вы уже участвуете в активном турнире. "
                        "Завершите его чтобы войти в очередь.")

        # Другой активный поиск
        for other in mm_search_store.get_by_guild(guild_id):
            if other.channel_id != search.channel_id:
                if interaction.user.id in other.player_user_ids.values():
                    return ("Вы уже в другой очереди. Выйдите из неё сначала. "
                            "Используйте /mm delete для удаления всех поисков.")

        # Тег сервера
        from config import GUILD_ID

        has_tag = interaction.user.primary_guild and interaction.user.primary_guild.id == GUILD_ID
        if not has_tag:
            return "Для входа в очередь нужен тег r!z3."

        # VAC — временная блокировка участия в турнирах
        from services import vac_service
        from services.vac_service import vac_blocked_text

        vac = await vac_service.get_active_vac(guild_id, interaction.user.id)
        if vac is not None:
            ts = int(vac["expires_at"].timestamp())
            return vac_blocked_text(ts)
        return None

    async def _ephemeral(self, interaction: discord.Interaction, text: str) -> None:
        """Ephemeral-ответ: followup, если interaction уже подтверждён."""
        try:
            is_done = interaction.response.is_done()
        except Exception:
            is_done = False
        try:
            if is_done:
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception as exc:
            logger.warning("MM ephemeral reply failed: %s", exc)

    async def _edit_panel(
        self, interaction: discord.Interaction, search: MMSearch
    ) -> None:
        """Обновить V2-панель очереди.

        После upfront defer edit_message недоступен — правим
        сообщение напрямую; без defer — через response (старое
        поведение, в т.ч. для тестовых fake-интеракций).
        """
        kwargs = dict(
            view=build_queue_panel(search),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            done = interaction.response.is_done()
        except Exception:
            done = False
        try:
            if done:
                await interaction.message.edit(**kwargs)
            else:
                await interaction.response.edit_message(**kwargs)
        except Exception as exc:
            logger.warning("Queue panel edit failed: %s", exc)

    async def handle_queue_join(
        self, interaction: discord.Interaction, guild_id: int, channel_id: int
    ) -> None:
        """Кнопка «Войти в очередь» на V2-панели."""
        # Подтверждаем interaction сразу — дальше могут быть долгие
        # операции (lock, канал, рендер PNG), иначе Discord ответит
        # Unknown interaction (10062) по истечении 3-секундного окна.
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        search = mm_search_store.get(guild_id, channel_id)
        if search is None:
            await self._ephemeral(interaction, "Поиск не найден.")
            return

        async with self._queue_lock(guild_id, channel_id):
            search = mm_search_store.get(guild_id, channel_id)
            if search is None:
                await self._ephemeral(interaction, "Поиск не найден.")
                return
            if search.is_locked:
                await self._ephemeral(
                    interaction,
                    "Идёт проверка готовности — набор закрыт. "
                    "Дождитесь результата матча.",
                )
                return
            if search.is_full:
                # Очередь заполнена, но матч не стартовал (ошибка
                # создания канала/карточки Ready Check). Это единственный
                # путь повторного запуска — иначе очередь «зависает».
                key = (guild_id, channel_id)
                elapsed = time.time() - self._begin_fail_at.get(key, 0.0)
                if elapsed < MATCH_START_RETRY_COOLDOWN:
                    wait = int(MATCH_START_RETRY_COOLDOWN - elapsed) + 1
                    await self._ephemeral(
                        interaction,
                        "Запуск матча уже завершился ошибкой — "
                        f"повторите через {wait} сек.",
                    )
                    return
                self._cancel_search_timer(guild_id, channel_id)
                rc, err = await self._begin_match(search, interaction)
                await self._update_queue_panel(search)
                if rc is None:
                    self._begin_fail_at[key] = time.time()
                    await self._ephemeral(
                        interaction,
                        err or "Не удалось запустить матч. Попробуйте ещё раз.",
                    )
                else:
                    self._begin_fail_at.pop(key, None)
                return

            error = await self._validate_join(interaction, search)
            if error:
                await self._ephemeral(interaction, error)
                return

            player_name = interaction.user.display_name
            if not search.add_player(player_name, interaction.user.id):
                await self._ephemeral(
                    interaction, "Не удалось добавить в очередь."
                )
                return

            if not search.timer_started:
                search.timer_started = True
                mm_search_store.set(search)
                self._start_search_timer(guild_id, channel_id)
            mm_search_store.set(search)

            if search.is_full:
                # Атомарно: lock → канал → ready-check (внутри _begin_match)
                key = (guild_id, channel_id)
                self._cancel_search_timer(guild_id, channel_id)
                rc, err = await self._begin_match(search, interaction)
                await self._update_queue_panel(search)
                if rc is None:
                    self._begin_fail_at[key] = time.time()
                    await self._ephemeral(
                        interaction,
                        err or "Не удалось запустить матч.",
                    )
                else:
                    self._begin_fail_at.pop(key, None)
                return

            await self._edit_panel(interaction, search)

    async def handle_queue_leave(
        self, interaction: discord.Interaction, guild_id: int, channel_id: int
    ) -> None:
        """Кнопка «Выйти из очереди» на V2-панели."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        search = mm_search_store.get(guild_id, channel_id)
        if search is None:
            await self._ephemeral(interaction, "Поиск не найден.")
            return

        async with self._queue_lock(guild_id, channel_id):
            search = mm_search_store.get(guild_id, channel_id)
            if search is None:
                await self._ephemeral(interaction, "Поиск не найден.")
                return
            if search.is_locked:
                await self._ephemeral(
                    interaction,
                    "Идёт проверка готовности — выход недоступен.",
                )
                return

            user_id = interaction.user.id
            name = next(
                (n for n, u in search.player_user_ids.items() if u == user_id),
                interaction.user.display_name,
            )
            if name not in search.players:
                await self._ephemeral(interaction, "Вы не в этой очереди.")
                return

            search.remove_player(name)
            if not search.players:
                search.timer_started = False
                self._cancel_search_timer(guild_id, channel_id)
            mm_search_store.set(search)
            await self._edit_panel(interaction, search)

    async def _begin_match(
        self, search: MMSearch, interaction: discord.Interaction
    ) -> tuple[ReadyCheck | None, str | None]:
        """Перевести заполненную очередь в ready-check.

        Порядок: lock state → match number → канал cs-4x4-match-N → RC.
        При любой ошибке очередь возвращается в open.
        """
        guild = interaction.guild
        match_id = f"rc_{search.guild_id}_{int(time.time() * 1000)}"
        try:
            match_num = await match_number_store.get_or_assign(match_id)
        except Exception as exc:
            logger.error("match number assign failed: %s", exc)
            match_num = int(time.time()) % 100000

        async def _revert() -> None:
            search.state = QUEUE_OPEN
            search.match_num = 0
            search.timer_started = bool(search.players)
            mm_search_store.set(search)
            # Перезапустить таймер, чтобы заполненная очередь не «зависла»
            if search.players:
                key = (search.guild_id, search.channel_id)
                task = self._search_timers.get(key)
                if task is None or task.done():
                    self._start_search_timer(search.guild_id, search.channel_id)
            await self._update_queue_panel(search)

        # 1. Блокируем очередь до создания канала/RC
        search.state = QUEUE_LOCKED_READY_CHECK
        search.match_num = match_num
        mm_search_store.set(search)
        await self._update_queue_panel(search)

        channel = None
        try:
            # 2. Временный канал матча
            channel, ch_err = await self._create_match_channel(guild, search, match_num)
            if channel is None:
                await _revert()
                if ch_err and ch_err.startswith("rate_limited:"):
                    secs = int(ch_err.split(":", 1)[1])
                    mins = max(1, secs // 60)
                    return None, (
                        f"Discord временно ограничил создание каналов "
                        f"(серверный rate-limit). Повторите через ~{mins} мин."
                    )
                detail = f" ({ch_err})" if ch_err else ""
                return None, f"Не удалось создать канал матча{detail}. Попробуйте ещё раз."

            # 3. Ready Check
            rc = await self._start_ready_check(
                search, interaction,
                match_id=match_id, match_num=match_num,
                match_channel_id=channel.id,
            )
        except Exception as exc:
            logger.error("_begin_match failed: %s", exc, exc_info=True)
            try:
                await self._delete_channel_by_id(channel.id if channel else 0)
            except Exception:
                pass
            await _revert()
            return None, "Не удалось запустить матч. Попробуйте ещё раз."

        if rc is None:
            await self._delete_channel_by_id(channel.id)
            await _revert()
            return None, "Не удалось запустить проверку готовности."
        return rc, None

    def _match_channel_overwrites(
        self, guild: discord.Guild, user_ids: set[int]
    ) -> dict:
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False,
                read_message_history=False,
            )
        }
        allow = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )
        for uid in user_ids:
            overwrites[discord.Object(id=uid)] = allow
        for role_id in (ORG_ROLE_ID, ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID):
            if role_id:
                overwrites[discord.Object(id=role_id)] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )
        return overwrites

    async def _pool_busy_channel_ids(self, guild_id: int) -> set[int]:
        """ID каналов пула, занятых активным RC или живым турниром."""
        busy: set[int] = set()
        try:
            for rc in await ready_check_store.list_active():
                cid = getattr(rc, "match_channel_id", 0) or 0
                if cid:
                    busy.add(cid)
        except Exception as exc:
            logger.warning("pool busy: ready checks failed: %s", exc)
        try:
            for t in store.get_by_guild(guild_id):
                if getattr(t, "phase", None) == TournamentPhase.COMPLETE:
                    continue
                for attr in ("channel_id", "mm_preset_channel_id"):
                    cid = getattr(t, attr, 0) or 0
                    if cid:
                        busy.add(cid)
        except Exception as exc:
            logger.warning("pool busy: tournaments failed: %s", exc)
        return busy

    async def _purge_channel(self, channel: discord.TextChannel) -> None:
        """Очистить историю канала пула перед переиспользованием."""
        try:
            for p in await channel.pins():
                try:
                    await p.unpin()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            while True:
                msgs = [m async for m in channel.history(limit=100)]
                if not msgs:
                    break
                try:
                    await channel.delete_messages(
                        msgs, reason="MM channel pool reset")
                except Exception:
                    for m in msgs:
                        try:
                            await m.delete()
                        except Exception:
                            pass
                if len(msgs) < 100:
                    break
        except Exception as exc:
            logger.warning("Pool channel purge failed %s: %s",
                           channel.id, exc)

    async def _acquire_pool_channel(
        self,
        guild: discord.Guild,
        user_ids: set[int],
        overwrites: dict,
    ) -> discord.TextChannel | None:
        """Занять свободный канал из пула: overwrites + очистка истории."""
        busy = await self._pool_busy_channel_ids(guild.id)
        for cid in MM_MATCH_CHANNEL_POOL:
            if cid in busy:
                continue
            channel = guild.get_channel(cid)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(cid)
                except Exception:
                    channel = None
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.edit(
                    overwrites=overwrites,
                    reason="MM match channel reuse",
                )
            except Exception as exc:
                logger.warning(
                    "Pool channel %s overwrites edit failed: %s", cid, exc)
                continue
            await self._purge_channel(channel)
            logger.info("MM match channel pool slot %s acquired", cid)
            return channel
        return None

    async def _create_match_channel(
        self, guild: discord.Guild, search: MMSearch, match_num: int
    ) -> tuple[discord.TextChannel | None, str | None]:
        """Канал матча: сначала пул постоянных, иначе временный create."""
        panel_channel = guild.get_channel(search.channel_id)
        if panel_channel is None:
            try:
                panel_channel = await self.bot.fetch_channel(search.channel_id)
            except Exception:
                panel_channel = None
        category = getattr(panel_channel, "category", None)
        if panel_channel is None:
            return None, "канал панели не найден"

        user_ids = {
            uid for uid in search.player_user_ids.values()
            if isinstance(uid, int) and uid > 0
        }
        overwrites = self._match_channel_overwrites(guild, user_ids)

        pooled = await self._acquire_pool_channel(guild, user_ids, overwrites)
        if pooled is not None:
            return pooled, None

        try:
            channel = await guild.create_text_channel(
                f"cs-4x4-match-{match_num}",
                category=category,
                overwrites=overwrites,
                reason="Ranked match channel",
            )
            return channel, None
        except discord.RateLimited as exc:
            logger.error(
                "Match channel rate limited: retry_after=%.0fs",
                exc.retry_after)
            return None, f"rate_limited:{max(1, int(exc.retry_after))}"
        except Exception as exc:
            logger.error("Failed to create match channel: %s", exc, exc_info=True)
            return None, str(exc)[:120]

    async def _delete_channel_by_id(self, channel_id: int) -> None:
        if not channel_id:
            return
        if channel_id in MM_MATCH_CHANNEL_POOL:
            return  # каналы пула переиспользуются, не удаляются
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None
        if channel is not None:
            try:
                await channel.delete(reason="Ready check finished")
            except Exception as exc:
                logger.warning("Failed to delete match channel %s: %s", channel_id, exc)

    async def _delete_match_channel(self, rc: ReadyCheck) -> None:
        """Удалить временный канал матча по ReadyCheck."""
        await self._delete_channel_by_id(getattr(rc, "match_channel_id", 0) or 0)

    async def _fetch_match_card_png(self, rc: ReadyCheck) -> bytes | None:
        """Скачать PNG карточки «Найден матч» с сообщения Ready Check."""
        if not rc.channel_id or not rc.message_id:
            return None
        try:
            channel = self.bot.get_channel(rc.channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(rc.channel_id)
            message = await channel.fetch_message(rc.message_id)
            if not message.attachments:
                return None
            attachment = message.attachments[0]
            return await attachment.read()
        except Exception as exc:
            logger.warning("Failed to fetch match card png: %s", exc)
            return None

    async def _recover_queues(self) -> None:
        """Восстановить панели очередей и снять зависшие блокировки."""
        await asyncio.sleep(10)
        try:
            for search in mm_search_store.all():
                try:
                    if search.message_id:
                        self.bot.add_view(
                            build_queue_controls(search.guild_id, search.channel_id),
                            message_id=search.message_id,
                        )
                except Exception as exc:
                    logger.warning("add_view for queue panel failed: %s", exc)

                if search.is_locked:
                    rc = await ready_check_store.get_active_by_search(
                        search.guild_id, search.channel_id
                    )
                    if rc is None:
                        # Блокировка без живого Ready Check — снимаем
                        search.state = QUEUE_OPEN
                        search.match_num = 0
                        search.timer_started = bool(search.players)
                        mm_search_store.set(search)
                        # Заполненная очередь без таймера — перезапустить
                        if search.is_full and search.players:
                            key = (search.guild_id, search.channel_id)
                            task = self._search_timers.get(key)
                            if task is None or task.done():
                                self._start_search_timer(
                                    search.guild_id, search.channel_id
                                )
                try:
                    await self._update_queue_panel(search)
                except Exception:
                    pass
            logger.info("Queue recovery done")
        except Exception as exc:
            logger.error("Queue recovery failed: %s", exc)

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
                        
                        # Обновить панель очереди
                        try:
                            await self._update_queue_panel(search)
                        except Exception as e:
                            logger.error(f"Error resetting search channel: {e}")

    async def on_tournament_complete(self, guild_id: int, channel_id: int) -> None:
        """Вызывается при завершении турнира для разблокировки канала."""
        if channel_id in LOCKED_CHANNELS:
            del LOCKED_CHANNELS[channel_id]
            logger.info(f"Channel {channel_id} unlocked after tournament completion")

    def cog_unload(self) -> None:
        """Отменить фоновые задачи cog при выгрузке."""
        for task in (
            self._rc_sweeper_task,
            self._rc_recovery_task,
            getattr(self, "_queue_recovery_task", None),
        ):
            if task is not None:
                task.cancel()
        for task in self._bg_tasks:
            task.cancel()


async def setup(bot: TournamentBot) -> None:
    """Настройка cog."""
    await bot.add_cog(MMCog(bot))
