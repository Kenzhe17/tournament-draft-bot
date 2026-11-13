"""Cog для matchmaking системы."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from utils.design import compose, text_card
from discord import app_commands
from discord.ext import commands

import config as _cfg
from config import (
    RANKED_READY_TIMEOUT_SECONDS, replace_emojis, ORG_ROLE_ID,
    ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID, MM_MATCH_CHANNEL_POOL,
)

# Единый канал для карточек «НАЙДЕН МАТЧ» (Ready Check)
MATCH_FOUND_CHANNEL_ID = _cfg.MATCH_FOUND_CHANNEL_ID

# Пауза между попытками запуска матча после ошибки (антиспам API)
MATCH_START_RETRY_COOLDOWN = 120.0
# Сколько считается «живым» запуск матча между LOCKED и Ready Check.
# Должен покрывать acquire канала + рендер PNG + Discord API с 429-retry.
_BEGIN_INFLIGHT_TTL = 300.0
from models.mm_search import (
    MMFormat, MMSearch, QUEUE_OPEN, QUEUE_LOCKED_READY_CHECK,
    QUEUE_WAITING_CHANNEL,
)
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
TOURNAMENT_CHANNEL_IDS = list(_cfg.TOURNAMENT_CHANNELS)

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


class _NoopResponse:
    """response.* для системного запуска матча без реального interaction."""

    async def defer(self, *a, **kw):
        return None

    async def send_message(self, *a, **kw):
        return None


class _NoopFollowup:
    async def send(self, *a, **kw):
        return None


class _SystemInteraction:
    """Минимальный shim discord.Interaction для запуска матча sweeper'ом.

    Используется когда очередь ждёт свободный канал пула и слот
    освободился — запускать должен бот, а не клик игрока.
    """

    def __init__(self, guild: discord.Guild, channel_id: int):
        self.guild = guild
        self.channel_id = channel_id
        self.user = guild.me
        self.response = _NoopResponse()
        self.followup = _NoopFollowup()

    @property
    def channel(self):
        return self.guild.get_channel(self.channel_id)


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
        # Идущие запуски матчей: (guild_id, search_channel_id) -> ts.
        # Sweeper не должен «разблокировать» поиск, пока _begin_match
        # между LOCKED и созданием Ready Check (секунды API-работы).
        self._begin_inflight: dict[tuple[int, int], float] = {}
        # Коалесинг UI-обновлений Ready Check: match_id -> поколение
        # правки и живая задача-воркер. Серия быстрых кликов «Готов»
        # схлопывается в одно актуальное обновление карточки.
        self._rc_ui_dirty: dict[str, int] = {}
        self._rc_ui_tasks: dict[str, asyncio.Task] = {}
        # Метрики задержек Ready Check (последние N значений, мс)
        self._ready_metrics: dict[str, deque] = {}
        self.bot.mm_cog = self  # Регистрация для доступа из view

    async def cog_load(self) -> None:
        """Загрузить очереди, запустить sweeper Ready Check и восстановление."""
        try:
            await mm_search_store.load()
        except Exception as exc:
            logger.error("MMSearchStore load failed: %s", exc)
        try:
            from services import match_channel_pool
            await match_channel_pool.ensure_slots()
            # FREE-слоты: @everyone не может писать в «пустой» канал пула
            asyncio.create_task(match_channel_pool.apply_resting_permissions(self.bot))
        except Exception as exc:
            logger.error("match channel pool init failed: %s", exc)
        self._rc_sweeper_task = asyncio.create_task(self._ready_check_sweeper())
        self._rc_recovery_task = asyncio.create_task(self._recover_ready_checks())
        self._queue_recovery_task = asyncio.create_task(self._recover_queues())

    mm_group = app_commands.Group(name="mm", description="Matchmaking поиск")

    @mm_group.command(name="squad", description="Создать поиск Squad (4x4, 8 игроков)")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_squad(self, interaction: discord.Interaction) -> None:
        """Создать поиск для формата Squad."""
        await self._create_search(interaction, MMFormat.SQUAD)

    @mm_group.command(
        name="freechannel",
        description="Принудительно освободить канал пула матчей (state → free)",
    )
    @app_commands.describe(channel_id="ID канала из пула — числом или упоминанием (опционально)")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_freechannel(
        self, interaction: discord.Interaction, channel_id: str = ""
    ) -> None:
        """Админ: проверить состояние слотов и освободить зависший канал.

        Действие журналируется. Канал в ERROR/RESERVED/CLEANING можно
        освободить; ACTIVE освобождается только после проверки — но
        команда всё равно выполняет release через менеджер (lockdown +
        purge), так что доступ старых игроков закрывается.
        """
        from services import match_channel_pool

        # Терпимый парсинг: «1569…», «<#1569…>», «channel_id:1569…»
        cid = 0
        if channel_id:
            import re

            runs = re.findall(r"\d{5,}", str(channel_id))
            if runs:
                cid = int(runs[-1])
            elif str(channel_id).strip().isdigit():
                cid = int(str(channel_id).strip())
        if str(channel_id).strip() and not cid:
            await interaction.response.send_message(
                "❌ Не удалось распознать ID канала — введите число.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await match_channel_pool.ensure_slots()
            slots = await match_channel_pool.list_slots()
            if cid:
                targets = [s for s in slots if s["channel_id"] == cid]
                if not targets:
                    await interaction.followup.send(
                        f"❌ Канал `{cid}` не найден в пуле.",
                        ephemeral=True,
                    )
                    return
            else:
                # Показать все не-free слоты и освободить их
                targets = [s for s in slots if s["state"] != "free"]
                if not targets:
                    await interaction.followup.send(
                        "✅ Все каналы пула свободны.", ephemeral=True,
                    )
                    return

            lines = []
            for s in targets:
                cid = s["channel_id"]
                old = dict(s)
                channel = interaction.guild.get_channel(cid)
                if channel is None:
                    try:
                        channel = await interaction.guild.fetch_channel(cid)
                    except Exception:
                        channel = None
                ok = False
                if isinstance(channel, discord.TextChannel):
                    try:
                        ok = await match_channel_pool.release_channel(channel)
                    except Exception as exc:
                        logger.error("force release %s failed: %s", cid, exc)
                if not ok:
                    prev = await match_channel_pool.force_release(cid)
                    logger.warning(
                        "FORCE RELEASE channel %s by admin %s: %s",
                        cid, interaction.user.id, prev,
                    )
                    ok = True
                state_now = (await match_channel_pool.slot_info(cid) or {}
                             ).get("state", "?")
                lines.append(
                    f"`{cid}`: {old['state']} (owner `{old['owner']}`) "
                    f"→ `{state_now}`"
                )
            await interaction.followup.send(
                "**Принудительное освобождение каналов**\n"
                + "\n".join(lines)
                + f"\n\nВыполнил: {interaction.user} (`{interaction.user.id}`)",
                ephemeral=True,
            )
        except Exception as exc:
            logger.error("mm freechannel failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                f"❌ Ошибка: {exc}", ephemeral=True,
            )

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

        # Defer сразу: дальше запись статистики + _begin_match (канал,
        # PNG) занимают секунды — без раннего ответа токен interaction
        # истекает (10062/10015).
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

        # Не создаём новый поиск — заполняем уже активный:
        # сначала в этом канале, иначе любой активный поиск сервера.
        search = mm_search_store.get(guild.id, interaction.channel_id)
        if search is None:
            searches = mm_search_store.get_by_guild(guild.id)
            search = searches[0] if searches else None
        if search is None:
            await interaction.followup.send(
                "❌ Нет активного поиска — /mmtest только дозаполняет "
                "существующий поиск.", ephemeral=True
            )
            return
        if await ready_check_store.get_active_by_search(
            guild.id, search.channel_id
        ):
            await interaction.followup.send(
                "❌ В канале этого поиска уже идёт проверка готовности.",
                ephemeral=True
            )
            return

        needed = search.required_players - len(search.players)
        if needed <= 0:
            await interaction.followup.send(
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
            search.start_new_session(time.time())
            search.timer_started = True
            self._start_search_timer(search.guild_id, search.channel_id)
        else:
            # Дозаполнение в активной сессии — лимит только растёт
            search.update_effective_limit()
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
                await interaction.followup.send(
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

        await interaction.followup.send(
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

    # ------------------------------------------------------------------
    # Уведомления о поиске (максимум 2 с пингом роли за сессию)
    # ------------------------------------------------------------------

    @staticmethod
    def _notification_due(search: MMSearch, kind: str, now: float) -> bool:
        """Условия отправки уведомления kind ('first'|'second').

        first:  >=120с от старта сессии И >=2 игрока в очереди.
        second: >=420с от старта, 5 <= count < required, первое
                уведомление подтверждённо отправлено ('sent') И
                >=120с прошло с момента ЕГО ФАКТИЧЕСКОЙ отправки
                (first_notification_sent_at, не плановое время).
        """
        if search.is_locked or not search.timer_started:
            return False
        count = len(search.players)
        required = search.required_players
        if not (1 <= count < required):
            return False
        elapsed = now - (search.session_started_at or 0.0)
        if kind == "first":
            return elapsed >= 120 and count >= 2
        if not (elapsed >= 420 and count >= 5):
            return False
        if getattr(search, "first_notification_status", "") != "sent":
            return False
        first_sent = getattr(search, "first_notification_sent_at", 0.0) or 0.0
        return first_sent > 0 and (now - first_sent) >= 120

    def _claim_notification(self, search: MMSearch, kind: str) -> bool:
        """Атомарно (под _queue_lock) зарезервировать отправку.

        pending -> sending. При suppress-cooldown статус становится
        'suppressed' (терминальный, без повторов).
        """
        attr = f"{kind}_notification_status"
        status = getattr(search, attr)
        if status != "pending":
            return False
        if time.time() < (search.notify_suppressed_until or 0.0):
            setattr(search, attr, "suppressed")
            mm_search_store.set(search)
            return False
        setattr(search, attr, "sending")
        mm_search_store.set(search)
        return True

    def _finish_notification(
        self, search: MMSearch, kind: str, message_id: int
    ) -> None:
        """Зафиксировать результат отправки (sent/failed)."""
        attr = f"{kind}_notification_status"
        if getattr(search, attr) != "sending":
            return
        if message_id:
            setattr(search, attr, "sent")
            setattr(search, f"{kind}_notification_sent_at", time.time())
            setattr(search, f"{kind}_notification_message_id", message_id)
        else:
            # Неопределённый результат — не рискуем повторным пингом
            # роли: failed терминален для этой сессии.
            setattr(search, attr, "failed")
        mm_search_store.set(search)

    async def _send_queue_notification(
        self, search: MMSearch, kind: str
    ) -> None:
        """Отправить embed-уведомление с пингом роли в канал
        уведомлений. Вызывать ТОЛЬКО после _claim_notification."""
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
            search_channel = self.bot.get_channel(search.channel_id)
            if search_channel is None:
                try:
                    search_channel = await self.bot.fetch_channel(
                        search.channel_id)
                except Exception as e:
                    logger.warning("MM notify: fetch search channel failed: %s", e)
            if not notification_channel or not search_channel:
                logger.warning(
                    "MM notify skipped: notify_ch=%s search_ch=%s",
                    notification_channel, search_channel)
                self._finish_notification(search, kind, 0)
                return
            # Упоминание роли — прямо в карточке уведомления
            # (TextDisplay внутри Container): в CV2 текст пингует
            # даже внутри контейнера. role.mention не нужен —
            # строка строится из ID, работает и при сбое get_role.
            role_mention = (
                f"<@&{NOTIFICATION_ROLE_ID}>"
                if NOTIFICATION_ROLE_ID else ""
            )

            count = len(search.players)
            required = search.required_players
            free = max(0, required - count)
            if kind == "first":
                desc = (
                    "Начался поиск игроков CS 4×4.\n\n"
                    f"Участников: {count}/{required}\n"
                    f"Свободных мест: {free}\n\n"
                    "Присоединяйтесь к поиску!\n"
                    f"{search_channel.mention}"
                )
            else:
                # «Матч почти собран» — текст по числу свободных мест
                if free <= 1:
                    tail = "Нужен последний игрок для запуска Ready Check."
                elif free == 2:
                    tail = "Присоединяйтесь к Ranked CS 4×4."
                else:
                    tail = (
                        f"До запуска матча осталось {free} игрока!"
                    )
                head = (
                    "Осталось всего одно место!"
                    if free <= 1 else
                    "Матч почти собран!"
                    if free <= 2 else
                    "Поиск игроков продолжается."
                )
                desc = (
                    f"{head}\n\n"
                    f"Участников: {count}/{required}\n"
                    f"Свободных мест: {free}\n\n"
                    f"{tail}\n"
                    f"{search_channel.mention}"
                )
            embed = discord.Embed(
                title="🔍 RANKED MATCHMAKING",
                description=desc,
                color=discord.Color.from_rgb(69, 69, 69)
            )
            # Повторная проверка актуальности перед отправкой:
            # между claim и send мог стартовать Ready Check,
            # истечь дедлайн или измениться состав — устаревший
            # пинг роли не отправляем (ТЗ §6.1).
            fresh = mm_search_store.get(search.guild_id, search.channel_id)
            attr = f"{kind}_notification_status"
            if (
                fresh is None
                or getattr(fresh, attr) != "sending"
                or fresh.is_locked
                or fresh.is_full
                or not self._notification_due(fresh, kind, time.time())
            ):
                logger.info(
                    "Queue notification %s aborted — session changed "
                    "before send (%s/%s)",
                    kind, search.guild_id, search.channel_id,
                )
                if fresh is not None and getattr(fresh, attr) == "sending":
                    self._finish_notification(search, kind, 0)
                return
            # Embed -> Container; упоминание роли — первой строкой
            # внутри карточки, чтобы пинг был частью сообщения,
            # а не отдельным content.
            from utils.design.adapter import embed_to_container

            card = embed_to_container(embed)
            if role_mention:
                kids = list(card.children)
                for child in kids:
                    card.remove_item(child)
                card.add_item(
                    discord.ui.TextDisplay(content=role_mention)
                )
                for child in kids:
                    card.add_item(child)
            layout = discord.ui.LayoutView(timeout=None)
            layout.add_item(card)
            msg = await notification_channel.send(
                view=layout,
                allowed_mentions=discord.AllowedMentions(
                    roles=True, users=False, everyone=False
                )
            )
            self._finish_notification(search, kind, msg.id)
        except Exception as e:
            logger.error(f"Error sending queue notification ({kind}): {e}")
            try:
                self._finish_notification(search, kind, 0)
            except Exception:
                pass

    async def _maybe_send_notifications(self, search: MMSearch) -> None:
        """Проверить и отправить Due-уведомления сессии.

        Должна вызываться под _queue_lock — claim атомарен,
        а перед отправкой состояние перечитывается вызывающим кодом.
        """
        now = time.time()
        for kind in ("first", "second"):
            attr = f"{kind}_notification_status"
            if getattr(search, attr) != "pending":
                continue
            if not self._notification_due(search, kind, now):
                continue
            if not self._claim_notification(search, kind):
                continue
            await self._send_queue_notification(search, kind)

    def _start_search_timer(self, guild_id: int, channel_id: int) -> None:
        """Динамический таймер поиска.

        Дедлайн = session_started_at + effective_limit_seconds —
        персистится в MMSearch и переживает рестарт. Лимит растёт
        с заполнением очереди (10→12→15→20 мин), но не уменьшается
        при выходе игроков. Уведомления: первое при elapsed>=120с
        И >=2 игроках; второе при elapsed>=420с И 5<=count<required
        И >=120с после фактической отправки первого — максимум два.
        """
        async def timer_task():
            try:
                while True:
                    search = mm_search_store.get(guild_id, channel_id)
                    if (
                        not search
                        or not search.players
                        or search.is_locked
                        or not search.timer_started
                    ):
                        return
                    now = time.time()
                    # Legacy/edge: сессия без started_at — стартуем сейчас
                    if not search.session_started_at:
                        search.start_new_session(now)
                        mm_search_store.set(search)
                    deadline = search.deadline_at
                    # Уведомления (claim атомарен под queue lock)
                    async with self._queue_lock(guild_id, channel_id):
                        fresh = mm_search_store.get(guild_id, channel_id)
                        if (
                            fresh is None
                            or not fresh.players
                            or fresh.is_locked
                            or not fresh.timer_started
                        ):
                            return
                        await self._maybe_send_notifications(fresh)
                    # Тайм-аут: дедлайн из сохранённого состояния
                    search = mm_search_store.get(guild_id, channel_id)
                    if search is None:
                        return
                    if not search.players or search.is_locked:
                        return
                    if not search.is_full and now >= search.deadline_at:
                        await self._expire_queue(guild_id, channel_id)
                        return
                    # Сон до ближайшего события (уведомление/дедлайн),
                    # максимум 30с — на случай изменений состояния.
                    delay = 30.0
                    first_sent = getattr(
                        search, "first_notification_sent_at", 0.0
                    ) or 0.0
                    marks = [
                        search.session_started_at + 120,  # first notify
                        search.session_started_at + 420,  # second notify
                        search.deadline_at,
                    ]
                    if first_sent:
                        # second_eligible_at = first_sent + 120с
                        marks.append(first_sent + 120)
                    for mark in marks:
                        if mark > now:
                            delay = min(delay, mark - now)
                    await asyncio.sleep(max(5.0, delay))
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error in search timer task: {e}")

        task = asyncio.create_task(timer_task())
        self._search_timers[(guild_id, channel_id)] = task

    async def _expire_queue(self, guild_id: int, channel_id: int) -> None:
        """Завершить сессию по дедлайну: кик игроков, сброс состояния.

        Под queue lock: перепроверяет актуальный дедлайн — если он
        был продлён (effective_limit вырос), поиск продолжается.
        """
        async with self._queue_lock(guild_id, channel_id):
            search = mm_search_store.get(guild_id, channel_id)
            if search is None or not search.players:
                return
            if search.is_locked or search.is_full:
                return
            if time.time() < search.deadline_at:
                return  # дедлайн продлён — таймер продолжит
            # Разблокировать канал
            if search.tournament_channel_id in LOCKED_CHANNELS:
                del LOCKED_CHANNELS[search.tournament_channel_id]
            # Завершить сессию: игроки и псевдонимы освобождаются,
            # уведомления сбрасываются — следующий вход = новая сессия
            search.players = []
            search.player_user_ids = {}
            search.aliases = {}
            search.timer_started = False
            search.tournament_channel_id = 0
            search.session_started_at = 0.0
            search.effective_limit_seconds = 0
            search.first_notification_status = "pending"
            search.first_notification_sent_at = 0.0
            search.first_notification_message_id = 0
            search.second_notification_status = "pending"
            search.second_notification_sent_at = 0.0
            search.second_notification_message_id = 0
            search.notify_suppressed_until = 0.0
            mm_search_store.set(search)
            try:
                await self._update_queue_panel(search)
            except Exception as e:
                logger.error(f"Error updating search timer message: {e}")
            logger.info(
                "MM queue expired: guild=%s channel=%s",
                guild_id, channel_id,
            )

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

            # Балансировка — Smart Balance 4×4; фолбэк на старый
            # алгоритм distribute_players_random_auto
            from utils.smart_balance import smart_balance_4v4
            teams = smart_balance_4v4(players_with_sr, search.player_user_ids)
            if teams is None:
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

    def _ready_metric(self, name: str, ms: float, match_id: str | None = None) -> None:
        """Записать замер задержки Ready Check (для диагностики)."""
        q = self._ready_metrics.setdefault(name, deque(maxlen=500))
        q.append(ms)
        logger.info(
            "ready_metric %s=%.1fms match_id=%s", name, ms, match_id
        )

    async def _update_ready_message(self, rc: ReadyCheck) -> None:
        """Обновить основное сообщение Ready Check (без спама).

        Карточка матча и панель очереди обновляются независимо:
        сбой одного не отменяет второй, каждый сегмент замеряется.
        """
        t0 = time.perf_counter()
        try:
            channel = self.bot.get_channel(rc.channel_id) or await self.bot.fetch_channel(rc.channel_id)
            if channel is None:
                return
            message = await channel.fetch_message(rc.message_id)
            disabled = rc.status != ReadyCheckStatus.READY_CHECK
            await message.edit(view=ready_card(rc, self.bot, disabled=disabled))
            self._ready_metric(
                "ready_message_update_ms",
                (time.perf_counter() - t0) * 1000, rc.match_id,
            )
        except discord.NotFound:
            logger.warning("Ready check message %s not found", rc.message_id)
        except Exception as exc:
            logger.error("Failed to update ready check message: %s", exc)

        # Обновить и панель очереди — живой счётчик готовности
        t1 = time.perf_counter()
        try:
            search = mm_search_store.get(rc.guild_id, rc.search_channel_id)
            if search is not None and search.is_locked:
                await self._update_queue_panel(search)
        except Exception:
            pass
        self._ready_metric(
            "ready_queue_update_ms",
            (time.perf_counter() - t1) * 1000, rc.match_id,
        )

    # ------------------------------------------------------ UI coalescing

    def _schedule_ready_ui(self, match_id: str) -> None:
        """Поставить обновление карточки Ready Check в фоновую очередь.

        Серия быстрых нажатий «Готов» увеличивает поколение — воркер
        перечитывает актуальное состояние и делает одно обновление,
        поэтому устаревший «2/8» не публикуется после «3/8».
        """
        self._rc_ui_dirty[match_id] = self._rc_ui_dirty.get(match_id, 0) + 1
        task = self._rc_ui_tasks.get(match_id)
        if task is None or task.done():
            task = asyncio.create_task(self._ready_ui_worker(match_id))
            self._rc_ui_tasks[match_id] = task
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _ready_ui_worker(self, match_id: str) -> None:
        """Фоновый воркер: debounce + актуальное состояние на момент flush."""
        processed = 0
        cancelled = False
        try:
            while True:
                await asyncio.sleep(0.25)  # окно склейки быстрых кликов
                dirty = self._rc_ui_dirty.get(match_id, 0)
                if dirty <= processed:
                    return
                processed = dirty
                rc = await ready_check_store.get(match_id)
                if rc is not None:
                    await self._update_ready_message(rc)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._rc_ui_tasks.pop(match_id, None)
            if not cancelled:
                try:
                    if self._rc_ui_dirty.get(match_id, 0) > processed:
                        # Пометили новую правку, пока воркер завершался
                        self._schedule_ready_ui(match_id)
                    else:
                        self._rc_ui_dirty.pop(match_id, None)
                except RuntimeError:
                    # Цикл событий уже закрывается (shutdown) —
                    # следующая правка создаст воркер заново.
                    pass

    def _spawn_ready_launch(self, match_id: str) -> None:
        """Запустить матч фоновой задачей (после ответа пользователю)."""
        task = asyncio.create_task(self._ready_launch_bg(match_id))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _ready_launch_bg(self, match_id: str) -> None:
        """Фоновый запуск: под _rc_lock, только если статус позволяет."""
        async with self._rc_lock(match_id):
            fresh = await ready_check_store.get(match_id)
            if fresh is not None and fresh.status in (
                ReadyCheckStatus.ALL_READY,
                ReadyCheckStatus.LAUNCHING,
            ):
                await self._launch_ready_check(fresh)

    async def _confirm_ready(
        self,
        match_id: str,
        user_id: int,
        timings: dict[str, float] | None = None,
    ) -> str:
        """Отметить игрока готовым и при 8/8 запустить матч. Возвращает текст ответа.

        Вызывать под self._rc_lock(match_id). Общий путь для кнопки и автотеста.
        Обновления Discord-сообщений НЕ выполняются синхронно — они
        ставятся в коалесцируемую фоновую очередь (_schedule_ready_ui),
        а запуск матча при 8/8 — в фоновую задачу (_spawn_ready_launch),
        поэтому пользовательский ответ не ждёт Discord API.
        timings — опциональный dict для сбора ready_pg_save_ms.
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
        t_save = time.perf_counter()
        try:
            await ready_check_store.save(rc)
        except Exception:
            # Не показывать ложное подтверждение: откатываем флаг
            player.is_ready = False
            player.ready_at = None
            raise
        if timings is not None:
            timings["pg_save_ms"] = (
                timings.get("pg_save_ms", 0.0)
                + (time.perf_counter() - t_save) * 1000
            )
        # Карточку и панель очереди обновит фоновый воркер —
        # пользовательский ответ от Discord API не зависит.
        self._schedule_ready_ui(match_id)

        if rc.ready_count >= len(rc.players):
            # Атомарный переход READY_CHECK -> ALL_READY -> LAUNCHING
            if await ready_check_store.try_transition(
                match_id, ReadyCheckStatus.READY_CHECK, ReadyCheckStatus.ALL_READY
            ):
                rc = await ready_check_store.get(match_id)
                rc.status = ReadyCheckStatus.LAUNCHING
                t_save = time.perf_counter()
                await ready_check_store.save(rc)
                if timings is not None:
                    timings["pg_save_ms"] = (
                        timings.get("pg_save_ms", 0.0)
                        + (time.perf_counter() - t_save) * 1000
                    )
                self._schedule_ready_ui(match_id)
                self._spawn_ready_launch(match_id)
                return "✅ Все игроки готовы — матч запускается."
        return f"✅ Готовность подтверждена. Ожидаем остальных игроков ({rc.ready_count}/8)."

    async def handle_ready_click(self, interaction: discord.Interaction, match_id: str) -> None:
        """Кнопка «Готов»."""
        t0 = time.perf_counter()
        try:
            # defer() без thinking → DEFERRED_MESSAGE_UPDATE: мгновенный
            # ack без индикатора «бот думает…».
            await interaction.response.defer()
        except Exception:
            pass
        ack_ms = (time.perf_counter() - t0) * 1000

        timings: dict[str, float] = {}
        t_lock = time.perf_counter()
        text: str
        try:
            async with self._rc_lock(match_id):
                lock_ms = (time.perf_counter() - t_lock) * 1000
                text = await self._confirm_ready(
                    match_id, interaction.user.id, timings
                )
        except Exception:
            logger.exception("Ready check confirm failed: %s", match_id)
            lock_ms = (time.perf_counter() - t_lock) * 1000
            text = "❌ Не удалось сохранить готовность. Попробуйте ещё раз."
        try:
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            pass
        total_ms = (time.perf_counter() - t0) * 1000

        self._ready_metric("ready_click_ack_ms", ack_ms, match_id)
        self._ready_metric("ready_lock_wait_ms", lock_ms, match_id)
        if "pg_save_ms" in timings:
            self._ready_metric(
                "ready_pg_save_ms", timings["pg_save_ms"], match_id
            )
        self._ready_metric("ready_user_response_ms", total_ms, match_id)

    async def _issue_mm_vacs(
        self, guild_id: int, user_ids, reason: str, event_key: str
    ) -> None:
        """VAC игрокам за срыв Ready Check — прогрессивная лестница.

        record_violation сам вычисляет уровень и срок; event_key
        (матч+тип нарушения) защищает от повторной выдачи за то же
        событие при дублирующихся Discord-callback/таймаутах.
        """
        from services.vac_service import record_violation
        issued_by = getattr(getattr(self.bot, "user", None), "id", 0) or 0
        for uid in {int(u) for u in user_ids if u and int(u) > 0}:
            try:
                row, err = await record_violation(
                    guild_id, uid, issued_by, reason,
                    event_key=f"{event_key}:{uid}")
                if row is None:
                    continue  # дубликат события или ошибка — не наказываем дважды
                logger.info(
                    "MM VAC issued: user=%s level=%s reason=%s",
                    uid, row.get("new_level"), reason)
                try:
                    user = self.bot.get_user(uid)
                    if user is None:
                        user = await self.bot.fetch_user(uid)
                    expires = row.get("expires_at")
                    ts = int(expires.timestamp()) if hasattr(
                        expires, "timestamp") else 0
                    issued = row.get("issued_at")
                    minutes = int(
                        (expires - issued).total_seconds() // 60
                    ) if expires and issued else 0
                    await user.send(
                        f"Вам выдан VAC {minutes} минут: {reason}.\n"
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
            await interaction.response.defer(ephemeral=True, thinking=True)
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
                "отказ от Ranked-матча",
                event_key=f"mm_decline:{rc.match_id}")

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
                    "не подтвердил готовность на матч",
                    event_key=f"mm_timeout:{rc.match_id}")
            await self._finish_cancelled(rc, keep_user_ids=keep)

    async def _finish_cancelled(self, rc: ReadyCheck, keep_user_ids: set[int]) -> None:
        """Вернуть игроков в очередь после отмены, удалить канал матча."""
        # Отменённый матч не должен съедать номер — освобождаем резерв,
        # следующий сыгранный матч получит этот номер.
        try:
            await match_number_store.release(rc.match_id)
        except Exception as exc:
            logger.warning("match number release failed: %s", exc)
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
        search.aliases = {n: a for n, a in search.aliases.items() if n in keep_names}
        search.state = QUEUE_OPEN
        search.match_num = 0
        # Возврат после неудачного Ready Check = НОВАЯ сессия:
        # новый started_at, лимит по числу вернувшихся. Авто-
        # уведомления роли подавлены на 30 минут — защита от
        # циклов пингов при повторных неудачных RC.
        now = time.time()
        if keep_names:
            search.start_new_session(now)
            search.suppress_notifications(now + 1800)
        else:
            search.timer_started = False
            search.session_started_at = 0.0
            search.effective_limit_seconds = 0
        search.timer_started = bool(keep_names)
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
        """8/8 — запустить существующий турнирный флоу ровно один раз.

        Обёртка с метрикой ready_launch_ms; вся логика — в _impl.
        """
        t0 = time.perf_counter()
        try:
            await self._launch_ready_check_impl(rc)
        finally:
            self._ready_metric(
                "ready_launch_ms",
                (time.perf_counter() - t0) * 1000, rc.match_id,
            )

    async def _launch_ready_check_impl(self, rc: ReadyCheck) -> None:
        """8/8 — запустить существующий турнирный флоу ровно один раз."""
        guild_id = rc.guild_id
        # Повторная проверка актуального статуса: объект rc мог устареть
        # (другой путь уже перевёл его в терминальный статус).
        fresh = await ready_check_store.get(rc.match_id)
        if fresh is None or fresh.status not in (
            ReadyCheckStatus.ALL_READY, ReadyCheckStatus.LAUNCHING,
        ):
            return
        rc = fresh
        try:
            # Дедупликация: турнир уже мог быть создан (retry/recovery).
            # Совпадением считается ТОЛЬКО тот же матч — тот же канал
            # матча (mm_preset_channel_id). Иначе второй матч с того же
            # канала поиска ошибочно пропускался, а его канал оставался
            # ACTIVE без карточки.
            dup_t = None
            rc_channel = getattr(rc, "match_channel_id", 0) or 0
            for t in store.get_by_guild(guild_id):
                if (
                    getattr(t, "mm_search_channel_id", 0) != rc.search_channel_id
                    or t.phase == TournamentPhase.COMPLETE
                ):
                    continue
                preset = getattr(t, "mm_preset_channel_id", 0) or 0
                if rc_channel and preset == rc_channel:
                    dup_t = t
                    break
                if not rc_channel and not preset:
                    dup_t = t
                    break
            if dup_t is not None:
                logger.info("Tournament already exists for search channel %s — skip launch", rc.search_channel_id)
                rc.status = ReadyCheckStatus.LAUNCHED
                await ready_check_store.save(rc)
                await self._update_ready_message(rc)
                # Догнать анонс матчей, если он не успел выполниться
                try:
                    guild = self.bot.get_guild(guild_id)
                    if guild is not None:
                        from services.match_announcer import announce_phase_matches
                        await announce_phase_matches(dup_t, guild, self.bot)
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

            # ЕДИНЫЙ снимок составов: берём команды из ReadyCheck
            # (player.user_id + team), а не пересчитываем баланс —
            # повторный smart_balance_4v4 недетерминирован при
            # равных метриках и давал другой состав, чем на
            # карточке «НАЙДЕН МАТЧ».
            teams = self._teams_from_ready_check(rc, search)
            if teams is None:
                logger.error(
                    "Invalid team snapshot for ready check %s — "
                    "match NOT launched", rc.match_id,
                )
                rc.status = ReadyCheckStatus.CANCELLED
                rc.cancel_reason = "невалидный снимок составов команд"
                await ready_check_store.save(rc)
                await self._update_ready_message(rc)
                await self._finish_cancelled(
                    rc, {p.user_id for p in rc.players}
                )
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

            await self._create_tournament_from_search(
                search, channel, teams=teams
            )

            rc.status = ReadyCheckStatus.LAUNCHED
            await ready_check_store.save(rc)
            await self._update_ready_message(rc)
            self._rc_locks.pop(rc.match_id, None)
            self._rc_ui_dirty.pop(rc.match_id, None)
            self._rc_ui_tasks.pop(rc.match_id, None)
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
                # снимаем блокировку и обновляем панель. Но ТОЛЬКО если
                # нет идущего запуска (_begin_match между LOCKED и RC)
                # и нет слота пула, закреплённого за этим поиском —
                # иначе разблокируем легитимный матч в процессе старта.
                try:
                    from services import match_channel_pool as _mcp

                    pool_slots = await _mcp.list_slots()
                except Exception:
                    pool_slots = []
                try:
                    for s in mm_search_store.all():
                        if s.state == QUEUE_WAITING_CHANNEL:
                            continue  # ждёт канал — обрабатывает sweeper ниже
                        if not s.is_locked:
                            continue
                        key = (s.guild_id, s.channel_id)
                        if time.time() - self._begin_inflight.get(key, 0.0) < _BEGIN_INFLIGHT_TTL:
                            continue  # _begin_match в процессе — не зависший
                        owner = f"mm:{s.guild_id}:{s.channel_id}"
                        if any(
                            sl.get("owner") == owner
                            and sl.get("state") in ("reserved", "active")
                            for sl in pool_slots
                        ):
                            continue  # слот пула зарезервирован под этот матч
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
                # WAITING_FOR_CHANNEL: матч ждёт свободный слот пула —
                # пробуем запустить при каждом проходе (дешёвый PG-чек,
                # реальный захват только если слот свободен)
                try:
                    for s in mm_search_store.all():
                        if s.state != QUEUE_WAITING_CHANNEL or not s.is_full:
                            continue
                        await self._try_start_waiting_match(s)
                except Exception as exc:
                    logger.error("Waiting-channel sweeper error: %s", exc)
                # Reconcile пула: ACTIVE/RESERVED без живого RC/турнира
                # старше 15 минут → ERROR (НЕ авто-free — решает админ
                # через /mm freechannel)
                self._sweep_count = getattr(self, "_sweep_count", 0) + 1
                if self._sweep_count % 60 == 0:
                    try:
                        await self._reconcile_pool_slots()
                    except Exception as exc:
                        logger.error("Pool reconcile error: %s", exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Ready check sweeper error: %s", exc)

    async def _try_start_waiting_match(self, search: MMSearch) -> None:
        """Повторная попытка запуска матча, ждущего свободный канал.

        Вызывается sweeper'ом; interaction заменяется системным shim'ом —
        все ephemeral-ответы игрокам не нужны, статус виден на панели.
        """
        guild = self.bot.get_guild(search.guild_id)
        if guild is None:
            return
        shim = _SystemInteraction(guild, search.tournament_channel_id
                                  or search.channel_id)
        rc, err = await self._begin_match(search, shim)
        await self._update_queue_panel(search)
        if rc is None and err:
            logger.info(
                "Waiting match %s/%s still blocked: %s",
                search.guild_id, search.channel_id, err,
            )

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

    @staticmethod
    def _teams_from_ready_check(
        rc: ReadyCheck, search: MMSearch
    ) -> tuple[list[str], list[str]] | None:
        """Восстановить составы команд из снимка Ready Check.

        ReadyCheck.players — единственный источник истины:
        каждый игрок привязан к Discord user_id и команде ("A"/"B"),
        порядок = порядок на карточке «НАЙДЕН МАТЧ» (капитан первым).
        Имена берём из очереди по user_id (не по display_name).

        Возвращает (team_a_names, team_b_names) либо None при
        нарушении целостности: !=8 уникальных ID, не 4+4, пересечения.
        """
        uid_to_name = {
            uid: name for name, uid in search.player_user_ids.items()
        }
        team_a: list[str] = []
        team_b: list[str] = []
        seen_uids: set[int] = set()
        for p in rc.players:
            uid = int(p.user_id)
            if uid in seen_uids:
                logger.error(
                    "Duplicate user_id %s in ready check %s",
                    uid, rc.match_id,
                )
                return None
            seen_uids.add(uid)
            name = uid_to_name.get(uid) or p.name
            if p.team == "B":
                team_b.append(name)
            else:
                team_a.append(name)
        if (
            len(rc.players) != 8
            or len(team_a) != 4
            or len(team_b) != 4
            or len(seen_uids) != 8
        ):
            logger.error(
                "Bad team snapshot in ready check %s: players=%d "
                "A=%d B=%d uids=%d",
                rc.match_id, len(rc.players),
                len(team_a), len(team_b), len(seen_uids),
            )
            return None
        # Все имена обязаны существовать в очереди поиска
        known = set(search.player_user_ids.keys())
        if any(n not in known for n in team_a + team_b):
            logger.error(
                "Team snapshot names missing from queue: rc=%s",
                rc.match_id,
            )
            return None
        return team_a, team_b

    async def _create_tournament_from_search(
        self,
        search: MMSearch,
        interaction_channel: discord.TextChannel,
        teams: tuple[list[str], list[str]],
    ) -> None:
        """Создать обычный турнир из поиска.

        teams — ОБЯЗАТЕЛЬНЫЙ снимок составов из Ready Check
        (player.user_id + team). Повторная балансировка для
        Ranked-матча запрещена: smart_balance_4v4 недетерминирован
        при равных метриках и уже выполнялся при создании RC.
        Вызывающий код (_launch_ready_check_impl) обязан проверить
        снимок через _teams_from_ready_check и отменить матч при
        невалидных данных — здесь fallback отсутствует намеренно.
        """
        if teams is None or len(teams) != 2:
            logger.error(
                "_create_tournament_from_search called without a "
                "valid team snapshot for search %s/%s — aborting",
                search.guild_id, search.channel_id,
            )
            return
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

        # Составы — ТОЛЬКО из снимка Ready Check. Никакой повторной
        # балансировки: smart_balance_4v4 недетерминирован при равных
        # метриках и уже выполнялся при создании карточки «НАЙДЕН
        # МАТЧ». Отсутствие/повреждение снимка обработано выше
        # (отмена матча + возврат игроков).
        team_lists: list[list[str]] = [list(teams[0]), list(teams[1])]

        # НЕ заполнять круги - команды создаются напрямую
        # Для random-auto круги не используются, только teams

        # Автоматически стартовать турнир и перейти к фазе матчей
        tournament.phase = TournamentPhase.QUALIFIERS
        tournament.is_test = True  # Отключить ставки для random-auto
        tournament.betting_open = False  # Явно отключить ставки

        # Создать команды из распределенных игроков
        tournament.teams = []
        for i, team_players in enumerate(team_lists):
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

        try:
            vac = await vac_service.check_vac(guild_id, interaction.user.id)
        except vac_service.VacStoreUnavailable:
            return "⚠️ Сервис VAC временно недоступен. Попробуйте позже."
        if vac is not None:
            ts = int(vac["expires_at"].timestamp())
            return vac_service.vac_blocked_text(
                ts, reason=vac.get("reason"), level=vac.get("level"))
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
        # thinking=True — у component-interaction ephemeral-defer
        # работает только в паре с ним.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

        begin_search: MMSearch | None = None
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
                if search.state == QUEUE_OPEN:
                    # Атомарный захват под lock: только один клик
                    # запускает матч, остальные видят locked выше.
                    search.state = QUEUE_LOCKED_READY_CHECK
                    mm_search_store.set(search)
                    self._cancel_search_timer(guild_id, channel_id)
                    begin_search = search
            else:
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
                    # Новая сессия: started_at, лимит, уведомления
                    search.start_new_session(time.time())
                    search.timer_started = True
                    mm_search_store.set(search)
                    self._start_search_timer(guild_id, channel_id)
                else:
                    # Динамический лимит: только растёт, не сбрасывается
                    search.update_effective_limit()
                mm_search_store.set(search)

                # Умное уведомление: второе может сработать сразу
                # при достижении 5 игроков после 5-й минуты (сценарий B)
                await self._maybe_send_notifications(search)

                if search.is_full:
                    # Атомарный захват: lock снят до тяжёлого _begin_match
                    search.state = QUEUE_LOCKED_READY_CHECK
                    mm_search_store.set(search)
                    self._cancel_search_timer(guild_id, channel_id)
                    begin_search = search
                else:
                    await self._edit_panel(interaction, search)
                    # Снимаем состояние «бот думает…» ephemeral-подтверждением
                    await self._ephemeral(
                        interaction,
                        f"✅ Вы в очереди "
                        f"({len(search.players)}/{search.required_players}).",
                    )
                    return

        # Вне lock: БД, канал, PNG, Ready Check — не блокируем очередь
        if begin_search is not None:
            key = (guild_id, channel_id)
            rc, err = await self._begin_match(begin_search, interaction)
            await self._update_queue_panel(begin_search)
            if rc is None:
                self._begin_fail_at[key] = time.time()
                await self._ephemeral(
                    interaction,
                    err or "Не удалось запустить матч. Попробуйте ещё раз.",
                )
            else:
                self._begin_fail_at.pop(key, None)
                await self._ephemeral(
                    interaction,
                    "🚀 Очередь заполнена — матч запускается!",
                )

    async def handle_queue_leave(
        self, interaction: discord.Interaction, guild_id: int, channel_id: int
    ) -> None:
        """Кнопка «Выйти из очереди» на V2-панели."""
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
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
                # Сессия завершена: следующий вход создаст новую
                # (новый started_at, лимит и уведомления)
                search.timer_started = False
                search.session_started_at = 0.0
                search.effective_limit_seconds = 0
                search.first_notification_status = "pending"
                search.first_notification_sent_at = 0.0
                search.first_notification_message_id = 0
                search.second_notification_status = "pending"
                search.second_notification_sent_at = 0.0
                search.second_notification_message_id = 0
                search.notify_suppressed_until = 0.0
                self._cancel_search_timer(guild_id, channel_id)
            else:
                # Выход НЕ сбрасывает достигнутый лимит и уведомления
                search.update_effective_limit()
            mm_search_store.set(search)
            # Проверка уведомлений и при выходе: если первый уже
            # отправлен и второй стал due (5+ игроков, >=7 мин,
            # >=120с после первого) — не ждём тик таймера.
            if search.timer_started and not search.is_locked:
                await self._maybe_send_notifications(search)
            await self._edit_panel(interaction, search)
            # Снимаем состояние «бот думает…» ephemeral-подтверждением
            await self._ephemeral(interaction, "✅ Вы вышли из очереди.")

    async def _begin_match(
        self, search: MMSearch, interaction: discord.Interaction
    ) -> tuple[ReadyCheck | None, str | None]:
        """Идемпотентная обёртка запуска матча.

        Один поиск = максимум один активный Ready Check и один
        идущий запуск. Повторные вызовы (клик, таймер, sweeper
        WAITING_FOR_CHANNEL, recovery) безопасно возвращают
        существующий процесс вместо создания дубля.
        """
        key = (search.guild_id, search.channel_id)
        existing = await ready_check_store.get_active_by_search(
            search.guild_id, search.channel_id
        )
        if existing is not None:
            logger.info(
                "_begin_match: active ready check %s exists for %s/%s — skip",
                existing.match_id, search.guild_id, search.channel_id,
            )
            return existing, None
        started = self._begin_inflight.get(key, 0.0)
        if time.time() - started < _BEGIN_INFLIGHT_TTL:
            return None, "Запуск матча уже выполняется — подождите."
        self._begin_inflight[key] = time.time()
        try:
            return await self._begin_match_impl(search, interaction)
        finally:
            self._begin_inflight.pop(key, None)

    async def _begin_match_impl(
        self, search: MMSearch, interaction: discord.Interaction
    ) -> tuple[ReadyCheck | None, str | None]:
        """Перевести заполненную очередь в ready-check.

        Порядок: lock state → match number → канал cs-4x4-match-N → RC.
        При любой ошибке очередь возвращается в open.
        """
        guild = interaction.guild
        match_id = f"rc_{search.guild_id}_{int(time.time() * 1000)}"
        # Повторная попытка (WAITING_FOR_CHANNEL и т.п.): search.match_num
        # уже содержит выданный номер — переиспользуем его, иначе каждый
        # ретрай сжигал бы новый номер и оставлял «дыру» в нумерации.
        prev_num = int(getattr(search, "match_num", 0) or 0)
        prev_key = str(getattr(search, "mm_num_key", "") or "")
        if prev_num and prev_key:
            try:
                await match_number_store.release(prev_key)
            except Exception:
                pass
            try:
                await match_number_store.assign(match_id, prev_num)
            except Exception:
                pass
            match_num = prev_num
        else:
            try:
                match_num = await match_number_store.get_or_assign(match_id)
            except Exception as exc:
                logger.error("match number assign failed: %s", exc)
                match_num = int(time.time()) % 100000
        search.mm_num_key = match_id

        async def _revert() -> None:
            # Матч не состоялся — освобождаем номер, чтобы он не
            # считался сыгранным и не оставлял пропуск в нумерации.
            try:
                await match_number_store.release(match_id)
            except Exception:
                pass
            search.state = QUEUE_OPEN
            search.match_num = 0
            try:
                delattr(search, "mm_num_key")
            except AttributeError:
                pass
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
                if ch_err == "pool_full":
                    # Все 5 слотов заняты — НЕ создаём временный канал.
                    # Матч ждёт в WAITING_FOR_CHANNEL; sweeper запустит
                    # его автоматически при освобождении слота.
                    search.state = QUEUE_WAITING_CHANNEL
                    mm_search_store.set(search)
                    await self._update_queue_panel(search)
                    return None, (
                        "Все каналы матчей сейчас заняты. "
                        "Матч начнётся автоматически, когда освободится слот."
                    )
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

    async def _create_match_channel(
        self, guild: discord.Guild, search: MMSearch, match_num: int
    ) -> tuple[discord.TextChannel | None, str | None]:
        """Канал матча через единый менеджер пула.

        Временные каналы не создаются: если все 5 слотов заняты,
        возвращается "pool_full" — матч ждёт в WAITING_FOR_CHANNEL.
        """
        from services import match_channel_pool

        user_ids = [
            uid for uid in search.player_user_ids.values()
            if isinstance(uid, int) and uid > 0
        ]
        role_ids = [
            r for r in (
                ORG_ROLE_ID, ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID,
            ) if r
        ]
        return await match_channel_pool.acquire_channel(
            guild, user_ids, role_ids=role_ids,
            owner=f"mm:{guild.id}:{search.channel_id}",
        )

    async def _delete_channel_by_id(self, channel_id: int) -> None:
        if not channel_id:
            return
        if channel_id in MM_MATCH_CHANNEL_POOL:
            # Канал пула не удаляем — возвращаем слот через менеджер
            from services import match_channel_pool
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    channel = None
            if isinstance(channel, discord.TextChannel):
                try:
                    await match_channel_pool.release_channel(channel)
                except Exception as exc:
                    logger.warning(
                        "Pool channel %s release failed: %s", channel_id, exc)
            return
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

    async def _reconcile_pool_slots(self) -> None:
        """Пометить ERROR слоты пула, на которые никто не ссылается.

        Слот считается «живым», если его channel_id есть в активном
        Ready Check или в не-завершённом турнире (channel_id,
        mm_preset_channel_id, match_channels). Без ссылок и старше
        15 минут → ERROR. Авто-освобождения нет: активный матч не
        роняем по таймеру, решение за админом (/mm freechannel).
        """
        from services import match_channel_pool
        from datetime import datetime, timezone

        slots = await match_channel_pool.list_slots()
        suspects = [
            s for s in slots
            if s["state"] in ("reserved", "active")
        ]
        if not suspects:
            return

        referenced: set[int] = set()
        try:
            for rc in await ready_check_store.list_active():
                cid = getattr(rc, "match_channel_id", 0) or 0
                if cid:
                    referenced.add(cid)
        except Exception as exc:
            logger.warning("reconcile: ready checks failed: %s", exc)
        try:
            for guild in self.bot.guilds:
                for t in store.get_by_guild(guild.id):
                    if getattr(t, "phase", None) == TournamentPhase.COMPLETE:
                        continue
                    for attr in ("channel_id", "mm_preset_channel_id"):
                        cid = getattr(t, attr, 0) or 0
                        if cid:
                            referenced.add(cid)
                    for cid in (t.match_channels or {}).keys():
                        try:
                            referenced.add(int(cid))
                        except (TypeError, ValueError):
                            pass
        except Exception as exc:
            logger.warning("reconcile: tournaments failed: %s", exc)

        now = datetime.now(timezone.utc)
        for s in suspects:
            cid = int(s["channel_id"])
            if cid in referenced:
                continue
            updated = s.get("updated_at")
            try:
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = (now - updated).total_seconds()
            except Exception:
                age = 0
            if age < 900:
                continue
            await match_channel_pool.mark_error(cid, s.get("owner", ""))
            logger.warning(
                "Pool slot %s marked ERROR: no live RC/tournament "
                "references it (state=%s owner=%s age=%.0fs)",
                cid, s["state"], s.get("owner", ""), age,
            )

    async def _recover_queues(self) -> None:
        """Восстановить панели очередей и снять зависшие блокировки."""
        await asyncio.sleep(10)
        try:
            # Слоты пула, закреплённые за MM-поисками — locked-очередь
            # с RESERVED/ACTIVE слотом НЕ считается зависшей (рестарт
            # мог случиться между резервированием канала и созданием RC)
            try:
                from services import match_channel_pool as _mcp
                pool_slots = await _mcp.list_slots()
            except Exception:
                pool_slots = []
            for search in mm_search_store.all():
                try:
                    if search.message_id:
                        self.bot.add_view(
                            build_queue_controls(search.guild_id, search.channel_id),
                            message_id=search.message_id,
                        )
                except Exception as exc:
                    logger.warning("add_view for queue panel failed: %s", exc)

                if search.state == QUEUE_LOCKED_READY_CHECK:
                    rc = await ready_check_store.get_active_by_search(
                        search.guild_id, search.channel_id
                    )
                    if rc is None:
                        owner = f"mm:{search.guild_id}:{search.channel_id}"
                        has_slot = any(
                            sl.get("owner") == owner
                            and sl.get("state") in ("reserved", "active")
                            for sl in pool_slots
                        )
                        if has_slot:
                            # Слот пула зарезервирован под этот матч —
                            # ждём RC/турнир, не разблокируем очередь
                            logger.info(
                                "Queue %s/%s locked with pool slot — skip unlock",
                                search.guild_id, search.channel_id,
                            )
                        else:
                            # Блокировка без живого Ready Check — снимаем
                            # (waiting_channel НЕ трогаем — это не зависание,
                            # sweeper канала сам запустит матч)
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
                # Активная открытая очередь с игроками: восстановить
                # сессию (started_at/лимит/уведомления) и таймер.
                # Дедлайн не сбрасывается — если он прошёл во время
                # простоя, таймер сразу завершит сессию.
                if (
                    search.state == QUEUE_OPEN
                    and search.players
                    and search.timer_started
                ):
                    if not search.session_started_at:
                        # Legacy-сессия без метки времени: даём
                        # полный лимит от момента восстановления
                        search.start_new_session(time.time())
                        mm_search_store.set(search)
                    else:
                        # Лимит мог вырасти до рестарта — не уменьшаем
                        search.update_effective_limit()
                        mm_search_store.set(search)
                    key = (search.guild_id, search.channel_id)
                    task = self._search_timers.get(key)
                    if task is None or task.done():
                        self._start_search_timer(
                            search.guild_id, search.channel_id
                        )
                    # Уведомления, наступившие во время простоя:
                    # отправляем только если условия ещё актуальны
                    # и уведомление не было отправлено ранее.
                    try:
                        async with self._queue_lock(
                            search.guild_id, search.channel_id
                        ):
                            fresh = mm_search_store.get(
                                search.guild_id, search.channel_id
                            )
                            if fresh is not None and not fresh.is_full:
                                await self._maybe_send_notifications(fresh)
                    except Exception as exc:
                        logger.warning(
                            "MM notify recovery failed %s/%s: %s",
                            search.guild_id, search.channel_id, exc,
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
