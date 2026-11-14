"""Cog для matchmaking системы (/mm squad).

Классический флоу r!z3: игроки встают в очередь → при наборе 8 бот
автоматически балансирует команды по SR, атомарно занимает один из
двух постоянных каналов пула и публикует там единственное CV2-сообщение
матча. Никаких Ready Check, PNG «Найден матч» и голосований за правила.

Дальше капитаны заполняют K/D ``K/D`` своих игроков, организатор
подтверждает победителя и счёт (Kazakhstan, до 5 побед) — views/mm_match_view.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import config as _cfg
from config import (
    replace_emojis, ORG_ROLE_ID,
    ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID, MM_MATCH_CHANNEL_POOL,
)

from models.mm_search import (
    MMFormat, MMSearch, QUEUE_OPEN, QUEUE_LOCKED_READY_CHECK,
    QUEUE_WAITING_CHANNEL,
)
from models.player_stats import PlayerStats
from models.tournament import (
    FormationMode, RegistrationState, TournamentSize, TournamentPhase,
    Tournament,
)
from storage.mm_search_store import mm_search_store
from storage.match_number_store import match_number_store
from storage.json_store import store
from storage.player_stats_store import player_stats_store
from views.mm_match_view import (
    MmMatchTemplateView,
    build_mm_match_view,
    match_id_of,
    meta_of,
)
from views.mm_search_view import (
    build_queue_controls,
    build_queue_panel,
)

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Каналы для турниров
TOURNAMENT_CHANNEL_IDS = list(_cfg.TOURNAMENT_CHANNELS)

# Заблокированные каналы для поиска
LOCKED_CHANNELS: dict[int, str] = {}  # channel_id -> search_channel_id

# Пауза между попытками запуска матча после ошибки (антиспам API)
MATCH_START_RETRY_COOLDOWN = 120.0
# Сколько считается «живым» запуск матча между LOCKED и созданием турнира.
_BEGIN_INFLIGHT_TTL = 300.0



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
        self._bg_tasks: set[asyncio.Task] = set()
        self._match_sweeper_task: asyncio.Task | None = None
        self._queue_recovery_task: asyncio.Task | None = None
        self._queue_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._begin_fail_at: dict[tuple[int, int], float] = {}
        # Идущие запуски матчей: (guild_id, search_channel_id) -> ts.
        self._begin_inflight: dict[tuple[int, int], float] = {}
        self.bot.mm_cog = self  # Регистрация для доступа из view

    async def cog_load(self) -> None:
        """Загрузить очереди, запустить sweeper матчей и восстановление."""
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
        self._match_sweeper_task = asyncio.create_task(self._match_sweeper())
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
        """Админ: проверить состояние слотов и освободить зависший канал."""
        from services import match_channel_pool

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
                channel = self.bot.get_channel(search.channel_id)
                if channel and search.message_id:
                    try:
                        message = await channel.fetch_message(search.message_id)
                        await message.delete()
                    except Exception:
                        pass

                mm_search_store.delete(search.guild_id, search.channel_id)
                deleted_count += 1
                self._cancel_search_timer(search.guild_id, search.channel_id)
            except Exception as e:
                logger.error(f"Error deleting search: {e}")

        await interaction.response.send_message(
            f"✅ Удалено {deleted_count} поисков на сервере.",
            ephemeral=True
        )

    @mm_group.command(name="test", description="Тест запуска матча с искусственными игроками")
    @app_commands.checks.has_permissions(administrator=True)
    async def mm_test(self, interaction: discord.Interaction) -> None:
        """Заполнить активный поиск тестовыми игроками и запустить матч."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("❌ Команда только на сервере.", ephemeral=True)
            return

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

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

        needed = search.required_players - len(search.players)
        if needed <= 0:
            await interaction.followup.send(
                "✅ Поиск уже заполнен.", ephemeral=True
            )
            return

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
            user_id = -(
                (guild.id % 100000) * 1_000_000
                + (search.channel_id % 1000) * 100
                + int(time.time()) % 100
            ) - added - 1
            stats = PlayerStats(
                guild_id=guild.id,
                user_id=user_id,
                name=name,
                elo=1200 + i * 100,
                wins=10 + i,
                games=20 + i * 2,
                finals=5,
                tournament_participations=20 + i * 2,
                total_kills=100 + i * 50,
                total_deaths=80 + i * 30,
                level=10 + i,
            )
            await player_stats_store.set(stats)
            if search.add_player(name, user_id):
                added += 1

        if not search.timer_started:
            search.start_new_session(time.time())
            search.timer_started = True
            self._start_search_timer(search.guild_id, search.channel_id)
        else:
            search.update_effective_limit()
        mm_search_store.set(search)

        try:
            await self._update_queue_panel(search)
        except Exception as exc:
            logger.error("mmtest: failed to refresh queue panel: %s", exc)

        if search.is_full:
            self._cancel_search_timer(search.guild_id, search.channel_id)
            tournament, err = await self._begin_match(search, interaction)
            if tournament is None:
                await interaction.followup.send(
                    err or "❌ Не удалось запустить матч.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"🧪 Тест: матч запущен в канале <#{tournament.channel_id}>.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"🧪 Добавлено {added} тестовых игроков — "
            f"в поиске {len(search.players)}/{search.required_players}.",
            ephemeral=True,
        )

    async def _create_search(self, interaction: discord.Interaction, format: MMFormat) -> None:
        """Создать новый поиск."""
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

        await interaction.response.send_message("Поиск создан!", ephemeral=True)

        message = await interaction.channel.send(
            view=build_queue_panel(search),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        search.message_id = message.id
        mm_search_store.set(search)

        try:
            self.bot.add_view(
                build_queue_controls(search.guild_id, search.channel_id),
                message_id=message.id,
            )
        except Exception as e:
            logger.warning("add_view for queue panel failed: %s", e)

        logger.info("Создан поиск %s в канале %s", format.value, interaction.channel_id)

    # ------------------------------------------------------------------
    # Уведомления о поиске (максимум 2 с пингом роли за сессию)
    # ------------------------------------------------------------------

    @staticmethod
    def _notification_due(search: MMSearch, kind: str, now: float) -> bool:
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
        attr = f"{kind}_notification_status"
        if getattr(search, attr) != "sending":
            return
        if message_id:
            setattr(search, attr, "sent")
            setattr(search, f"{kind}_notification_sent_at", time.time())
            setattr(search, f"{kind}_notification_message_id", message_id)
        else:
            setattr(search, attr, "failed")
        mm_search_store.set(search)

    async def _send_queue_notification(
        self, search: MMSearch, kind: str
    ) -> None:
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
                if free <= 1:
                    tail = "Нужен последний игрок для запуска матча."
                elif free == 2:
                    tail = "Присоединяйтесь к Ranked CS 4×4."
                else:
                    tail = f"До запуска матча осталось {free} игрока!"
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
        """Динамический таймер поиска (как раньше)."""
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
                    if not search.session_started_at:
                        search.start_new_session(now)
                        mm_search_store.set(search)
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
                    search = mm_search_store.get(guild_id, channel_id)
                    if search is None:
                        return
                    if not search.players or search.is_locked:
                        return
                    if not search.is_full and now >= search.deadline_at:
                        await self._expire_queue(guild_id, channel_id)
                        return
                    delay = 30.0
                    first_sent = getattr(
                        search, "first_notification_sent_at", 0.0
                    ) or 0.0
                    marks = [
                        search.session_started_at + 120,
                        search.session_started_at + 420,
                        search.deadline_at,
                    ]
                    if first_sent:
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
        async with self._queue_lock(guild_id, channel_id):
            search = mm_search_store.get(guild_id, channel_id)
            if search is None or not search.players:
                return
            if search.is_locked or search.is_full:
                return
            if time.time() < search.deadline_at:
                return
            if search.tournament_channel_id in LOCKED_CHANNELS:
                del LOCKED_CHANNELS[search.tournament_channel_id]
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
        key = (guild_id, channel_id)
        if key in self._search_timers:
            self._search_timers[key].cancel()
            del self._search_timers[key]

    # ------------------------------------------------------------------
    # Запуск матча: баланс → канал пула → турнир → CV2-карточка
    # ------------------------------------------------------------------

    @staticmethod
    def _live_mm_tournament(
        guild_id: int,
        search_channel_id: int,
        match_num: int = 0,
    ) -> Tournament | None:
        """Активный MM-турнир этого поиска.

        ``match_num > 0`` — только турнир того же поколения запуска
        (meta.match_num в карточке); ``0`` — любой активный турнир
        поиска (для reconcile/unlock-проверок).
        """
        for t in store.get_by_guild(guild_id):
            if (
                getattr(t, "mm_search_channel_id", 0) == search_channel_id
                and t.phase != TournamentPhase.COMPLETE
            ):
                if match_num:
                    meta = meta_of(t) or {}
                    if meta.get("match_num") != match_num:
                        continue
                return t
        return None

    @classmethod
    def _pending_mm_tournament(cls, search: MMSearch) -> Tournament | None:
        """Турнир текущего поколения запуска (search.match_num).

        Поколение 0 (очередь ещё не запускалась) → None: параллельные
        матчи из одной очереди разрешены, старые активные турниры
        поиска НЕ блокируют новый запуск.
        """
        if not search.match_num:
            return None
        return cls._live_mm_tournament(
            search.guild_id, search.channel_id, search.match_num
        )

    async def _match_sweeper(self) -> None:
        """Фоновая задача: запуск отложенных матчей + reconcile пула."""
        while True:
            try:
                await asyncio.sleep(10)
                # WAITING_FOR_CHANNEL / восстановление после рестарта:
                # заполненные очереди без живого матча — пробуем старт.
                try:
                    for s in mm_search_store.all():
                        if not s.is_full:
                            continue
                        if self._pending_mm_tournament(s) is not None:
                            continue
                        key = (s.guild_id, s.channel_id)
                        if time.time() - self._begin_inflight.get(key, 0.0) < _BEGIN_INFLIGHT_TTL:
                            continue
                        await self._try_start_waiting_match(s)
                except Exception as exc:
                    logger.error("Match sweeper launch error: %s", exc)
                # Self-heal: locked-очередь без живого матча и без
                # идущего запуска — снимаем блокировку.
                try:
                    for s in mm_search_store.all():
                        if not s.is_locked:
                            continue
                        # Ждущая канала заполненная очередь — легитимное
                        # состояние, sweeper loop1 сам перезапустит матч.
                        if s.is_waiting_channel and s.is_full:
                            continue
                        if s.match_num and self._live_mm_tournament(
                            s.guild_id, s.channel_id, s.match_num
                        ) is not None:
                            continue
                        key = (s.guild_id, s.channel_id)
                        if time.time() - self._begin_inflight.get(key, 0.0) < _BEGIN_INFLIGHT_TTL:
                            continue
                        # Зависший запуск без живого матча: заполненная
                        # очередь → WAITING (sweeper дошлёт), пустая → OPEN.
                        if s.is_full:
                            s.state = QUEUE_WAITING_CHANNEL
                        else:
                            s.state = QUEUE_OPEN
                            s.match_num = 0
                        s.timer_started = bool(s.players) and not s.is_full
                        mm_search_store.set(s)
                        await self._update_queue_panel(s)
                        logger.warning(
                            "Stuck MM queue restored: %s/%s",
                            s.guild_id, s.channel_id,
                        )
                except Exception as exc:
                    logger.error("Queue unlock sweeper error: %s", exc)
                # Retry неудавшихся освобождений каналов и дозапись
                # финализационных маркеров (mark_done/release могли
                # упасть при сбое БД/Discord API).
                try:
                    await self._retry_pending_channel_releases()
                except Exception as exc:
                    logger.error("Channel release retry error: %s", exc)
                try:
                    await self._reconcile_mm_finalizations()
                except Exception as exc:
                    logger.error("Finalize reconcile error: %s", exc)
                # Reconcile пула: ACTIVE/RESERVED без живого турнира
                # старше 15 минут → ERROR (НЕ авто-free)
                self._sweep_count = getattr(self, "_sweep_count", 0) + 1
                if self._sweep_count % 60 == 0:
                    try:
                        await self._reconcile_pool_slots()
                    except Exception as exc:
                        logger.error("Pool reconcile error: %s", exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Match sweeper error: %s", exc)

    async def _try_start_waiting_match(self, search: MMSearch) -> None:
        """Попытка запуска матча, ждущего свободный канал пула."""
        guild = self.bot.get_guild(search.guild_id)
        if guild is None:
            return
        shim = _SystemInteraction(guild, search.tournament_channel_id
                                  or search.channel_id)
        tournament, err = await self._begin_match(search, shim)
        await self._update_queue_panel(search)
        if tournament is None and err:
            logger.info(
                "Waiting match %s/%s still blocked: %s",
                search.guild_id, search.channel_id, err,
            )

    async def _begin_match(
        self, search: MMSearch, interaction: discord.Interaction
    ) -> tuple[Tournament | None, str | None]:
        """Идемпотентная обёртка запуска матча.

        Один поиск = максимум один активный MM-турнир и один идущий
        запуск. Повторные вызовы (клик, таймер, sweeper) безопасно
        возвращают существующий процесс вместо создания дубля.
        """
        key = (search.guild_id, search.channel_id)
        # Идемпотентность по поколению запуска: блокируем только
        # повтор того же match_num. Поколение 0 (новый набор) не
        # блокируется старыми активными матчами этого поиска —
        # параллельные матчи из одной очереди разрешены.
        existing = self._pending_mm_tournament(search)
        if existing is not None:
            logger.info(
                "_begin_match: live MM tournament %s exists for %s/%s — skip",
                existing.id, search.guild_id, search.channel_id,
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
    ) -> tuple[Tournament | None, str | None]:
        """Запустить MM-матч: баланс по SR → пул каналов → CV2-карточка.

        При полном пуле очередь уходит в WAITING_FOR_CHANNEL —
        sweeper запустит матч при освобождении слота.
        """
        guild = interaction.guild
        if guild is None:
            return None, "Команда только на сервере."

        match_id = f"mm_{search.guild_id}_{search.channel_id}_{int(time.time() * 1000)}"
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
            if search.players:
                key = (search.guild_id, search.channel_id)
                task = self._search_timers.get(key)
                if task is None or task.done():
                    self._start_search_timer(search.guild_id, search.channel_id)
            await self._update_queue_panel(search)

        search.state = QUEUE_LOCKED_READY_CHECK
        search.match_num = match_num
        mm_search_store.set(search)
        await self._update_queue_panel(search)

        channel = None
        tournament: Tournament | None = None
        try:
            # 1. Балансировка 4×4 по SR (K/D не участвует — только SR)
            players_with_sr: list[tuple[str, float]] = []
            for name in search.players:
                user_id = search.player_user_ids.get(name, 0)
                stats = (
                    await player_stats_store.get(guild.id, user_id)
                    if user_id else None
                )
                players_with_sr.append(
                    (name, stats.skill_rating if stats else 0.0)
                )

            from utils.smart_balance import smart_balance_4v4
            teams = smart_balance_4v4(players_with_sr, search.player_user_ids)
            if teams is None:
                probe = Tournament(
                    guild_id=search.guild_id,
                    channel_id=interaction.channel_id,
                    size=TournamentSize.EIGHT,
                    formation_mode=FormationMode.RANDOM_AUTO,
                )
                teams = probe.distribute_players_random_auto(players_with_sr)
            if len(teams) != 2 or any(len(t) != 4 for t in teams):
                raise RuntimeError(
                    f"Unexpected team split: {[len(t) for t in teams]}"
                )

            # 2. Атомарный слот пула + права + очистка канала
            channel, ch_err = await self._acquire_match_channel(guild, search)
            if channel is None:
                if ch_err == "pool_full":
                    search.state = QUEUE_WAITING_CHANNEL
                    mm_search_store.set(search)
                    await self._update_queue_panel(search)
                    return None, (
                        "Все каналы матчей сейчас заняты. "
                        "Матч начнётся автоматически, когда освободится слот."
                    )
                await _revert()
                detail = f" ({ch_err})" if ch_err else ""
                return None, f"Не удалось подготовить канал матча{detail}. Попробуйте ещё раз."

            # 3. Турнир: одна финальная пара, без ставок и регистрации
            tournament = Tournament(
                guild_id=search.guild_id,
                channel_id=channel.id,
                size=TournamentSize.EIGHT,
                formation_mode=FormationMode.RANDOM_AUTO,
                organizer_id=0,
            )
            tournament.mm_search_channel_id = search.channel_id
            tournament.mm_preset_channel_id = channel.id
            # id = guild_id_мс: два параллельных MM-матча могут попасть
            # в одну миллисекунду → коллизия match_id/finalize_lock.
            # Суффикс поколения делает id уникальным.
            if any(
                t.id == tournament.id
                for t in store.get_by_guild(search.guild_id)
            ):
                tournament.id = (
                    f"{tournament.id}_{search.channel_id}_{match_num}"
                )
            tournament.registration = RegistrationState.CLOSED
            tournament.is_test = True
            tournament.betting_open = False
            tournament.phase = TournamentPhase.FINAL
            tournament.final_teams = [0, 1]

            team_lists = [list(teams[0]), list(teams[1])]
            tournament.teams = []
            for i, names in enumerate(team_lists):
                tournament.teams.append({
                    "players": names,
                    "captain": names[0] if names else None,
                    "team_number": i,
                })
                for name in names:
                    tournament.players_pool.append(name)
                    tournament.player_user_ids[name] = (
                        search.player_user_ids.get(name, 0)
                    )

            # 4. Номер матча в единой нумерации
            try:
                await match_number_store.assign(
                    match_id_of(tournament), match_num
                )
            except Exception as exc:
                logger.warning("match number pre-assign failed: %s", exc)

            # 5. Meta карточки матча (снимок ростера для проверки
            #    неизменности состава при финализации)
            tournament.match_channels[str(channel.id)] = {
                "match_type": "final",
                "match_index": 0,
                "channel_id": channel.id,
                "card_message_id": 0,
                "started_at": time.time(),
                "match_num": match_num,
                "state": "active",
                "status_text": "ожидается",
                "roster": [n for names in team_lists for n in names],
                "kd": {},
            }
            tournament.message_id = 0
            store.set(tournament)

            # 6. Единственное CV2-сообщение матча в канале пула
            msg = await channel.send(
                view=build_mm_match_view(tournament, match_num),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            meta = meta_of(tournament)
            if meta is not None:
                meta["card_message_id"] = msg.id
                store.set(tournament)

        except Exception as exc:
            logger.error("_begin_match failed: %s", exc, exc_info=True)
            # Турнир мог быть создан до сбоя (канал/карточка) —
            # удаляем сироту, иначе она навсегда держит слот пула.
            if tournament is not None:
                try:
                    store.delete(tournament.guild_id, tournament.channel_id)
                except Exception:
                    try:
                        tournament.phase = TournamentPhase.COMPLETE
                        store.set(tournament)
                    except Exception:
                        pass
            try:
                if channel is not None:
                    await self._release_pool_channel(channel)
            except Exception:
                pass
            await _revert()
            return None, "Не удалось запустить матч. Попробуйте ещё раз."

        # Поиск отработал — очередь снова открыта
        search.clear()
        search.state = QUEUE_OPEN
        search.match_num = 0
        for attr in ("mm_num_key",):
            try:
                delattr(search, attr)
            except AttributeError:
                pass
        mm_search_store.set(search)

        try:
            await self._update_queue_panel(search)
        except Exception as e:
            logger.error(f"Error updating queue panel: {e}")

        if tournament is not None:
            if tournament_channel_id := getattr(
                search, "tournament_channel_id", 0
            ):
                LOCKED_CHANNELS.pop(tournament_channel_id, None)
        logger.info(
            "MM match #%s launched: tournament=%s channel=%s",
            match_num, getattr(tournament, "id", "?"),
            getattr(channel, "id", "?"),
        )
        return tournament, None

    async def _acquire_match_channel(
        self, guild: discord.Guild, search: MMSearch
    ) -> tuple[discord.TextChannel | None, str | None]:
        """Захватить слот пула под матч (8 игроков + ORG-роли)."""
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
        # owner включает match_num поколения: параллельные матчи одной
        # очереди различимы, release не снимет слот с чужого матча.
        return await match_channel_pool.acquire_channel(
            guild, user_ids, role_ids=role_ids,
            owner=(
                f"mm:{guild.id}:{search.channel_id}:"
                f"{int(getattr(search, 'match_num', 0) or 0)}"
            ),
        )

    async def _release_pool_channel(self, channel: discord.TextChannel) -> None:
        from services import match_channel_pool

        try:
            await match_channel_pool.release_channel(channel)
        except Exception as exc:
            logger.warning(
                "Pool channel %s release failed: %s", channel.id, exc)

    async def _delete_channel_by_id(self, channel_id: int) -> None:
        if not channel_id:
            return
        if channel_id in MM_MATCH_CHANNEL_POOL:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    channel = None
            if isinstance(channel, discord.TextChannel):
                await self._release_pool_channel(channel)
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None
        if channel is not None:
            try:
                await channel.delete(reason="MM match aborted")
            except Exception as exc:
                logger.warning("Failed to delete match channel %s: %s", channel_id, exc)

    # ------------------------------------------------------------------
    # Очередь: join/leave, панель
    # ------------------------------------------------------------------

    def _queue_lock(self, guild_id: int, channel_id: int) -> asyncio.Lock:
        key = (guild_id, channel_id)
        lock = self._queue_locks.get(key)
        if lock is None:
            lock = self._queue_locks[key] = asyncio.Lock()
        return lock

    async def _update_queue_panel(self, search: MMSearch) -> None:
        if not search.message_id:
            return
        channel = self.bot.get_channel(search.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(search.channel_id)
            except Exception:
                return
        try:
            message = await channel.fetch_message(search.message_id)
            await message.edit(
                view=build_queue_panel(search),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            logger.warning("Queue panel message %s not found", search.message_id)
        except Exception as exc:
            logger.error("Failed to update queue panel: %s", exc)

    async def _validate_join(
        self, interaction: discord.Interaction, search: MMSearch
    ) -> str | None:
        guild_id = search.guild_id

        for tournament in store.get_by_guild(guild_id):
            if tournament.is_player_active(interaction.user.id):
                return ("Вы уже участвуете в активном турнире. "
                        "Завершите его чтобы войти в очередь.")

        for other in mm_search_store.get_by_guild(guild_id):
            if other.channel_id != search.channel_id:
                if interaction.user.id in other.player_user_ids.values():
                    return ("Вы уже в другой очереди. Выйдите из неё сначала. "
                            "Используйте /mm delete для удаления всех поисков.")

        from config import GUILD_ID

        has_tag = interaction.user.primary_guild and interaction.user.primary_guild.id == GUILD_ID
        if not has_tag:
            return "Для входа в очередь нужен тег r!z3."

        from services import vac_service

        vac = await vac_service.check_vac(guild_id, interaction.user.id)
        if vac is not None:
            ts = int(vac["expires_at"].timestamp())
            return vac_service.vac_blocked_text(
                ts, reason=vac.get("reason"), level=vac.get("level"))
        return None

    async def _ephemeral(self, interaction: discord.Interaction, text: str) -> None:
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
                if search.is_waiting_channel:
                    await self._ephemeral(
                        interaction,
                        "Все каналы матчей заняты — ваш матч начнётся "
                        "автоматически при освобождении слота.",
                    )
                else:
                    await self._ephemeral(
                        interaction,
                        "Идёт запуск матча — набор закрыт. "
                        "Дождитесь результата.",
                    )
                return
            if search.is_full:
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
                    search.start_new_session(time.time())
                    search.timer_started = True
                    mm_search_store.set(search)
                    self._start_search_timer(guild_id, channel_id)
                else:
                    search.update_effective_limit()
                mm_search_store.set(search)

                await self._maybe_send_notifications(search)

                if search.is_full:
                    search.state = QUEUE_LOCKED_READY_CHECK
                    mm_search_store.set(search)
                    self._cancel_search_timer(guild_id, channel_id)
                    begin_search = search
                else:
                    await self._edit_panel(interaction, search)
                    await self._ephemeral(
                        interaction,
                        f"✅ Вы в очереди "
                        f"({len(search.players)}/{search.required_players}).",
                    )
                    return

        if begin_search is not None:
            key = (guild_id, channel_id)
            tournament, err = await self._begin_match(begin_search, interaction)
            await self._update_queue_panel(begin_search)
            if tournament is None:
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
                    "Идёт запуск или ожидание канала — выход недоступен.",
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
                search.update_effective_limit()
            mm_search_store.set(search)
            if search.timer_started and not search.is_locked:
                await self._maybe_send_notifications(search)
            await self._edit_panel(interaction, search)
            await self._ephemeral(interaction, "✅ Вы вышли из очереди.")

    async def _reconcile_pool_slots(self) -> None:
        """Пометить ERROR слоты пула, на которые никто не ссылается.

        Слот считается «живым», если его channel_id есть в активном
        MM-турнире (channel_id / mm_preset_channel_id / match_channels).
        Без ссылок и старше 15 минут → ERROR. Авто-освобождения нет.
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
                "Pool slot %s marked ERROR: no live tournament "
                "references it (state=%s owner=%s age=%.0fs)",
                cid, s["state"], s.get("owner", ""), age,
            )

    async def _retry_pending_channel_releases(self) -> None:
        """Повторить освобождение слота для завершённых MM-матчей,
        у которых release не был подтверждён (meta.channel_released).

        _release_match_channel сам проверяет owner поколения и
        фактический state слота — чужой/уже свободный слот не трогает.
        """
        from views.mm_match_view import _release_match_channel, meta_of

        for guild in self.bot.guilds:
            for t in store.get_by_guild(guild.id):
                try:
                    if not getattr(t, "mm_search_channel_id", 0):
                        continue
                    if t.phase != TournamentPhase.COMPLETE:
                        continue
                    meta = meta_of(t) or {}
                    if (
                        meta.get("state") != "done"
                        or meta.get("channel_released")
                    ):
                        continue
                    await _release_match_channel(guild, t)
                except Exception as exc:
                    logger.warning(
                        "channel release retry failed for %s: %s",
                        getattr(t, "id", "?"), exc,
                    )

    async def _reconcile_mm_finalizations(self) -> None:
        """Дозаписать match_finalizations='done' для MM-матчей,
        у которых все этапы завершены (meta.state='done'), но маркер
        остался 'stats_done' — mark_done упал при сбое БД.

        ELO/монеты/история НЕ повторяются: stats_done уже подтверждает
        применение статистики, здесь только финальный маркер.
        """
        from storage import finalize_store
        from views.mm_match_view import match_id_of, meta_of

        for guild in self.bot.guilds:
            for t in store.get_by_guild(guild.id):
                try:
                    if not getattr(t, "mm_search_channel_id", 0):
                        continue
                    if t.phase != TournamentPhase.COMPLETE:
                        continue
                    meta = meta_of(t) or {}
                    if (
                        meta.get("state") != "done"
                        or meta.get("finalize_marker_done")
                    ):
                        continue
                    mid = match_id_of(t)
                    st = await finalize_store.get_status(guild.id, mid)
                    if st == "stats_done":
                        await finalize_store.mark_done(guild.id, mid)
                        meta["finalize_marker_done"] = True
                        store.set(t)
                        logger.info(
                            "MM finalize marker repaired: %s", mid)
                    elif st == "done":
                        meta["finalize_marker_done"] = True
                        store.set(t)
                except Exception as exc:
                    logger.warning(
                        "finalize reconcile failed for %s: %s",
                        getattr(t, "id", "?"), exc,
                    )

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
                    # Турнир текущего поколения (match_num) — живой
                    # матч, очередь корректно закрыта до его конца.
                    live = (
                        self._live_mm_tournament(
                            search.guild_id, search.channel_id,
                            search.match_num,
                        )
                        if search.match_num else None
                    )
                    # WAITING_CHANNEL — легитимное ожидание слота пула:
                    # сохраняем игроков и match_num, sweeper перезапустит
                    # матч автоматически после освобождения канала.
                    waiting_full = (
                        search.is_waiting_channel and search.is_full
                    )
                    if live is None and not waiting_full:
                        # Блокировка без живого матча — аварийное
                        # состояние: заполненная очередь → WAITING
                        # (sweeper дошлёт), иначе → OPEN.
                        if search.is_full:
                            search.state = QUEUE_WAITING_CHANNEL
                        else:
                            search.state = QUEUE_OPEN
                            search.match_num = 0
                        search.timer_started = (
                            bool(search.players) and not search.is_full
                        )
                        mm_search_store.set(search)
                        if search.is_full and search.players:
                            key = (search.guild_id, search.channel_id)
                            task = self._search_timers.get(key)
                            if task is None or task.done():
                                self._start_search_timer(
                                    search.guild_id, search.channel_id
                                )
                if (
                    search.state == QUEUE_OPEN
                    and search.players
                    and search.timer_started
                ):
                    if not search.session_started_at:
                        search.start_new_session(time.time())
                        mm_search_store.set(search)
                    else:
                        search.update_effective_limit()
                        mm_search_store.set(search)
                    key = (search.guild_id, search.channel_id)
                    task = self._search_timers.get(key)
                    if task is None or task.done():
                        self._start_search_timer(
                            search.guild_id, search.channel_id
                        )
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
                tournament_channel = self.bot.get_channel(search.tournament_channel_id)
                if tournament_channel:
                    tournament = store.get(guild_id, tournament_channel.id)
                    if not tournament or tournament.phase == TournamentPhase.COMPLETE:
                        if search.tournament_channel_id in LOCKED_CHANNELS:
                            del LOCKED_CHANNELS[search.tournament_channel_id]
                        search.tournament_channel_id = 0
                        mm_search_store.set(search)
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
            self._match_sweeper_task,
            getattr(self, "_queue_recovery_task", None),
        ):
            if task is not None:
                task.cancel()
        for task in self._bg_tasks:
            task.cancel()


async def setup(bot: TournamentBot) -> None:
    """Настройка cog."""
    await bot.add_cog(MMCog(bot))
