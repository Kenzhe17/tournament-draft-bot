"""Точка входа — Discord-бот для турнирного драфта."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import os
import time

import discord
from discord.ext import commands

from config import DATABASE_URL, DISCORD_TOKEN, GUILD_ID, guild_allowed, replace_emojis
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


# ---------------------------------------------------------------------------
# Дедупликация обновлений главного сообщения турнира.
#
# Таймерные циклы (betting/draft) вызывают update_tournament_message
# каждые 5 секунд на каждый активный турнир. Раньше каждый вызов
# делал fetch_message + message.edit — Discord API получал десятки
# избыточных правок в минуту при неизменном контенте.
#
# Теперь: контент (embed + view) рендерится, вычисляется стабильная
# сигнатура; message.edit выполняется только при реальном изменении.
# Анонсирование матчей (announce_phase_matches) идемпотентно и
# вызывается на каждый апдейт — пропускать его при неизменном
# embed нельзя (смена фазы обязана анонсировать матчи).
# ---------------------------------------------------------------------------

_EDIT_LOCKS: dict[str, asyncio.Lock] = {}
_EDIT_LOCKS_MAX = 200
# channel_key -> {"sig": str, "message": discord.Message}
_EDIT_CACHE: dict[str, dict] = {}
_EDIT_CACHE_MAX = 200


def _edit_lock(key: str) -> asyncio.Lock:
    lock = _EDIT_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _EDIT_LOCKS[key] = lock
        if len(_EDIT_LOCKS) > _EDIT_LOCKS_MAX:
            _EDIT_LOCKS.pop(next(iter(_EDIT_LOCKS)))
    return lock


def _component_sig(item, parts: list[str]) -> None:
    """Рекурсивно собрать сигнатуру компонентов View/LayoutView."""
    parts.append(
        "|".join(
            str(getattr(item, attr, ""))
            for attr in (
                "custom_id", "label", "disabled", "style", "emoji",
                "content", "placeholder", "value", "title", "description",
            )
        )
    )
    for child in getattr(item, "children", []) or []:
        _component_sig(child, parts)


def _message_signature(embed, view) -> str:
    """Стабильная сигнатура отображаемого контента сообщения.

    Любое изменение embed (title/description/fields/image) или
    компонентов view (кнопки, тексты, disabled) меняет сигнатуру —
    пропустить edit можно только когда НИЧЕГО видимого не изменилось.
    """
    parts = [embed.to_dict().__repr__()]
    if view is not None:
        _component_sig(view, parts)
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


# P1.2: дешёвый «предварительный» кэш для таймерных тиков. Позволяет
# пропустить build_embed + SQL-запросы, когда отображаемое состояние
# турнира не менялось. Ключ — tournament id (не channel): у турнира
# одно главное сообщение.
_TOURNAMENT_FP_CACHE: dict[str, dict] = {}
_TOURNAMENT_FP_CACHE_MAX = 200
# Максимальный возраст кэша: страховка от PG-изменений, которые не
# проходят через update_tournament_message (например расчёт ставок).
_TIMER_REBUILD_TTL = 60.0


def _tournament_display_fingerprint(tournament) -> str:
    """Сигнатура состояния, влияющего на отображаемый embed.

    Дешёвая (без SQL и без Discord API): сериализация to_dict()
    турнира + betting-поля, которых нет в to_dict, и флаг
    is_betting_open() — он зависит от времени и ловит момент
    закрытия окна ставок без изменения объекта.
    """
    try:
        extras = {
            "betting_open": bool(getattr(tournament, "betting_open", False)),
            "betting_phase": getattr(tournament, "betting_phase", None),
            "betting_results": getattr(tournament, "betting_results", {}),
            "betting_open_now": bool(tournament.is_betting_open()),
        }
        payload = repr((tournament.to_dict(), extras))
    except Exception:
        # Не смогли сериализовать — считаем состояние «грязным»,
        # пусть идёт полный rebuild.
        return ""
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _pets_for_members(rows, member_ids):
    """Отфильтровать строки pets по членству владельца в гильдии.

    pets не имеют guild_id — принадлежность определяется по
    owner_1_id/owner_2_id. owner_2_id может быть NULL.
    """
    for row in rows:
        if (
            row["owner_1_id"] in member_ids
            or row["owner_2_id"] in member_ids
        ):
            yield row


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
        # Мониторинг производительности (ENABLE_PERF_METRICS).
        try:
            from utils import perf_metrics

            perf_metrics.start(self)

            def _ranked_active():
                try:
                    return sum(
                        1
                        for t in store.all()
                        if getattr(t, "mm_search_channel_id", 0)
                    )
                except Exception:
                    return None

            def _pg_pool():
                try:
                    import storage.db as dbmod

                    pool = dbmod._pool
                    if pool is None:
                        return None
                    return {
                        "size": pool.get_size(),
                        "idle": pool.get_idle_size(),
                        "max": pool.get_max_size(),
                    }
                except Exception:
                    return None

            perf_metrics.register_gauge("ranked_matches", _ranked_active)
            perf_metrics.register_gauge("pg_pool", _pg_pool)
        except Exception as exc:
            logger.warning("perf_metrics init failed: %s", exc)

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
        await self.load_extension("cogs.games.stats")
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
        await self.load_extension("cogs.wardrobe")
        await self.load_extension("cogs.emoji")
        await self.load_extension("cogs.reputation")
        await self.load_extension("cogs.info")
        await self.load_extension("cogs.vac")
        await self.load_extension("cogs.banner")
        await self.load_extension("cogs.banner_admin")

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

        # Startup-валидация серверной конфигурации.
        # Обязательные переменные → feature отключена без них.
        # Необязательные → fallback/автосоздание, не пугаем ERROR.
        # Формат ORG_ROLE_MAP: "channel_id:role_id,..." (csv).
        try:
            import config as _cfg

            # (env attr, env var, функция, обязательна?, fallback)
            _ENV_VARS = (
                ("GUILD_ID", "GUILD_ID", "привязка к серверу/tag-check", True, "нет"),
                ("TOURNAMENT_CHANNELS", "TOURNAMENT_CHANNELS", "каналы организаторов", True, "нет"),
                ("ORG_ROLE_MAP", "ORG_ROLE_MAP", "роли организаторов по каналам (cid:rid,...)", True, "нет"),
                ("NOTIFICATION_CHANNEL_ID", "NOTIFICATION_CHANNEL_ID", "анонсы турниров", False, "канал матчей/турниров"),
                ("MM_MATCH_CHANNEL_POOL", "MM_MATCH_CHANNEL_POOL", "пул каналов MM-матчей", False, "create/delete каналов на матч"),
                ("REP_LOG_CHANNEL_ID", "REP_LOG_CHANNEL_ID", "публичный лог репутации", False, "канал создаётся автоматически"),
                ("WELCOME_CHANNEL_ID", "WELCOME_CHANNEL_ID", "приветствия новым участникам", False, "welcome отключён"),
            )
            missing_required = []
            missing_optional = []
            for attr, env_name, feature, required, fallback in _ENV_VARS:
                if getattr(_cfg, attr):
                    continue
                entry = f"{env_name} ({feature}; fallback: {fallback})"
                (missing_required if required else missing_optional).append(entry)
            for entry in missing_required:
                logger.warning(
                    "Обязательная переменная не задана — функция "
                    "отключена: %s. Задайте в Railway Variables.",
                    entry,
                )
            if missing_optional:
                logger.info(
                    "Необязательные переменные не заданы (fallback "
                    "или отключено): %s",
                    "; ".join(missing_optional),
                )
        except Exception as exc:
            logger.warning("Config validation failed: %s", exc)

        # Startup-диагностика окружения (без секретов).
        try:
            import os as _os

            commit = (
                _os.getenv("RAILWAY_GIT_COMMIT_SHA")
                or _os.getenv("GIT_COMMIT_SHA")
                or "unknown"
            )
            env_name = (
                _os.getenv("RAILWAY_ENVIRONMENT_NAME")
                or _os.getenv("APP_ENV")
                or "local/unknown"
            )
            logger.info("Startup: env=%s commit=%s", env_name, commit[:10])
        except Exception as exc:
            logger.warning("Startup diagnostics failed: %s", exc)

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
                    if not guild_allowed(guild_id):
                        continue
                    try:
                        guild_synced = await self.tree.sync(guild=discord.Object(id=guild_id))
                        logger.info(f"Синхронизировано {len(guild_synced)} команд для сервера {guild_id}")
                    except Exception as e:
                        logger.error(f"Ошибка синхронизации для сервера {guild_id}: {e}")
            except Exception as e:
                logger.error(f"Ошибка при получении серверов из БД: {e}")

        # Force sync для основного сервера (env GUILD_ID; 0/не разрешён — пропуск)
        if GUILD_ID and guild_allowed(GUILD_ID):
            try:
                guild_synced = await self.tree.sync(guild=discord.Object(id=GUILD_ID))
                logger.info(f"Синхронизировано {len(guild_synced)} команд для основного сервера {GUILD_ID}")
            except Exception as e:
                logger.error(f"Ошибка синхронизации для основного сервера: {e}")

        # Internal economy bridge for r!z3 Arcade (disabled unless
        # ECONOMY_BRIDGE_TOKEN/PORT are configured; runs on this event loop)
        try:
            from services.economy_bridge import start_bridge
            await start_bridge()
        except Exception as e:
            logger.error(f"Failed to start economy bridge: {e}", exc_info=True)

        # Store bot instance globally for logging
        from models.tournament import set_bot_instance
        set_bot_instance(self)

        # Критическая проверка целостности JSON-хранилища турниров:
        # при повреждённых данных без валидного backup стор в
        # unhealthy-режиме и не перезаписывает файл — требуются
        # действия администратора.
        try:
            if not store.is_healthy():
                from config import DATA_FILE
                logger.critical(
                    "tournaments.json повреждён и не восстановлен — "
                    "сохранения отключены. Проверьте %s и *.bak/*.corrupt-*",
                    DATA_FILE,
                )
        except Exception as exc:
            logger.warning("JSON store health check failed: %s", exc)

        # Восстановление persistent views активных турниров.
        for tournament in store.all():
            view = self.build_view_for_tournament(tournament)
            self._register_view(view)

        # MM-матчи: persistent DynamicItem-шаблоны кнопок карточки
        # матча (K/D, победитель, статистика, отмена, правила).
        try:
            from views.mm_match_view import MmMatchTemplateView

            self.add_view(MmMatchTemplateView())
        except Exception as exc:
            logger.warning("MM match view registration failed: %s", exc)

        # Кнопка «📜 Правила» в карточках анонсов матчей турнира.
        try:
            from services.match_announcer import MatchRulesView

            self.add_view(MatchRulesView())
        except Exception as exc:
            logger.warning("Match rules view registration failed: %s", exc)


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
        self,
        guild: discord.Guild,
        tournament: Tournament,
        *,
        from_timer: bool = False,
    ) -> None:
        """Отредактировать главное сообщение турнира.

        Оптимизация P1: контент рендерится и сравнивается по
        сигнатуре — message.edit выполняется только при реальном
        изменении отображаемых данных. Сериализация правок одного
        сообщения — через per-channel lock. Анонс матчей фазы
        идемпотентен и вызывается на каждый апдейт.

        P1.2: при from_timer=True (вызовы из betting/draft loops)
        сначала считается дешёвый fingerprint состояния турнира.
        Если он совпадает с кэшем и кэшу меньше TTL — пропускаем
        build_embed и все SQL-запросы, но всё равно выполняем
        announce_phase_matches (он дешёвый и нужен для анонса
        матчей при смене фазы). Внешние вызовы (кнопки, модалы,
        ставки) идут с from_timer=False — они всегда делают
        полный rebuild, поэтому изменения от пользователя
        отражаются немедленно.
        """
        if not tournament.message_id:
            logger.warning("Нет message_id для сервера %s", guild.id)
            return

        fp_key = str(tournament.id)
        fp = _tournament_display_fingerprint(tournament) if from_timer else ""
        now = time.monotonic()
        if from_timer and fp:
            cached_fp = _TOURNAMENT_FP_CACHE.get(fp_key)
            if (
                cached_fp is not None
                and cached_fp.get("fp") == fp
                and now - cached_fp.get("ts", 0.0) < _TIMER_REBUILD_TTL
            ):
                # Состояние не менялось — embed не строим, edit не
                # нужен. Анонс матчей всё равно проверяем.
                try:
                    from services.match_announcer import (
                        announce_phase_matches,
                    )

                    await announce_phase_matches(tournament, guild, self)
                except Exception as exc:
                    logger.error("Match announce failed: %s", exc)
                return

        if from_timer and fp:
            if len(_TOURNAMENT_FP_CACHE) >= _TOURNAMENT_FP_CACHE_MAX:
                _TOURNAMENT_FP_CACHE.pop(next(iter(_TOURNAMENT_FP_CACHE)))
            _TOURNAMENT_FP_CACHE[fp_key] = {"fp": fp, "ts": now}

        key = f"{guild.id}:{tournament.channel_id}"
        async with _edit_lock(key):
            channel = guild.get_channel(tournament.channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(tournament.channel_id)
                except discord.HTTPException as exc:
                    logger.error("Канал не найден: %s", exc)
                    return

            from utils import perf_metrics

            async with perf_metrics.async_timing("embed_build_ms"):
                embed = await build_embed_for_phase(tournament, guild)
            async with perf_metrics.async_timing("view_build_ms"):
                view = self.build_view_for_tournament(tournament)
                self._register_view(view)
                if (
                    tournament.phase == TournamentPhase.DRAFT
                    and view is not None
                    and hasattr(view, "enrich_with_sr")
                ):
                    try:
                        await view.enrich_with_sr(guild.id)
                    except Exception:
                        logger.debug(
                            "draft SR enrich failed", exc_info=True)
                payload = compose(embed, view)
                sig = _message_signature(embed, payload)

            cached = _EDIT_CACHE.get(key)
            message = cached.get("message") if cached else None

            if cached is None or cached.get("sig") != sig or message is None:
                # Контент изменился (или сообщение ещё не закэшировано)
                # — редактируем. Кэшированный Message.edit не требует
                # fetch_message: экономим один API-вызов на апдейт.
                if message is None:
                    try:
                        async with perf_metrics.async_timing(
                            "msg_fetch_ms"
                        ):
                            message = await channel.fetch_message(
                                tournament.message_id
                            )
                        perf_metrics.incr("discord_fetches")
                    except discord.NotFound:
                        logger.warning(
                            "Сообщение %s не найдено (возможно удалено)",
                            tournament.message_id,
                        )
                        _EDIT_CACHE.pop(key, None)
                        message = None
                    except discord.HTTPException as exc:
                        logger.warning("Не удалось получить сообщение: %s", exc)
                        message = None

                if message is not None:
                    try:
                        async with perf_metrics.async_timing(
                            "msg_edit_ms"
                        ):
                            await message.edit(
                                view=payload,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                    except discord.NotFound:
                        # Сообщение удалили между кэшем и правкой —
                        # инвалидируем кэш, следующий апдейт сделает
                        # fetch_message заново.
                        logger.warning(
                            "Сообщение %s удалено во время edit",
                            tournament.message_id,
                        )
                        _EDIT_CACHE.pop(key, None)
                        message = None
                    except discord.Forbidden as exc:
                        logger.error(
                            "Нет прав на редактирование сообщения "
                            "турнира %s: %s", tournament.id, exc
                        )
                    except discord.HTTPException as exc:
                        logger.error(
                            "Не удалось обновить сообщение: %s", exc
                        )
                    else:
                        perf_metrics.incr("discord_edits")
                        if len(_EDIT_CACHE) >= _EDIT_CACHE_MAX:
                            _EDIT_CACHE.pop(next(iter(_EDIT_CACHE)))
                        _EDIT_CACHE[key] = {"sig": sig, "message": message}
            # else: sig совпал — контент не изменился, edit пропущен.

        # Анонс старта матчей активной фазы в канал результатов —
        # идемпотентно (is_announced), выполняется на каждый апдейт,
        # чтобы не пропустить анонс при смене фазы.
        try:
            from services.match_announcer import announce_phase_matches

            async with perf_metrics.async_timing("msg_announce_ms"):
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
                    if not guild_allowed(guild.id):
                        continue
                    # Get all tournaments for this guild
                    tournaments = store.get_by_guild(guild.id)
                    for tournament in tournaments:
                        if not tournament:
                            continue

                        tournament_key = f"{guild.id}_{tournament.betting_phase}"

                        if tournament.is_betting_open():
                            # Update message to show countdown
                            await self.update_tournament_message(
                                guild, tournament, from_timer=True
                            )
                            # Reset the updated flag since betting is open again
                            if tournament_key in updated_after_close:
                                updated_after_close.remove(tournament_key)
                        elif tournament.betting_phase_start_time is not None:
                            # Betting was open but now closed - update once to remove timer
                            if tournament_key not in updated_after_close:
                                await self.update_tournament_message(
                                    guild, tournament, from_timer=True
                                )
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
                    if not guild_allowed(guild.id):
                        continue
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
                            await self.update_tournament_message(
                                guild, tournament, from_timer=True
                            )
                        else:
                            # Time expired - make random pick
                            logger.info(f"Draft timer expired for guild {guild.id}, making random pick")
                            random_pick = tournament.pick_random_player()
                            if random_pick:
                                picker_pos, player = random_pick
                                tournament.pick_player(picker_pos, player)
                                draft_complete = tournament.advance_after_pick()
                                # store.set — синхронный файловый I/O,
                                # выносим из event loop.
                                await asyncio.to_thread(store.set, tournament)

                                # Update tournament message
                                await self.update_tournament_message(
                                    guild, tournament, from_timer=True
                                )

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
                                        await asyncio.to_thread(
                                            store.set, tournament
                                        )
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

                # P5: pets не имеют guild_id — вместо запроса с
                # ANY(все member ids) на каждую гильдию (G запросов
                # с большими массивами) загружаем таблицу pets один
                # раз и фильтруем по членству в Python. Таблица pets
                # мала (питомцы — штучный контент), а
                # check_fullness_and_energy перечитывает pet по id,
                # поэтому «устаревшая» строка после удаления
                # безопасно превращается в no-op.
                from storage.pet_store import pet_store
                from storage.db import get_pool

                pool = await get_pool()
                async with pool.acquire() as conn:
                    all_pet_rows = await conn.fetch("SELECT * FROM pets")

                # Get all guilds
                for guild in self.guilds:
                    if not guild_allowed(guild.id):
                        continue
                    try:
                        member_ids = {m.id for m in guild.members}
                        for row in _pets_for_members(all_pet_rows, member_ids):
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

                from zoneinfo import ZoneInfo
                now_almaty = datetime.now(ZoneInfo("Asia/Almaty"))
                today = now_almaty.date()
                # Срабатываем один раз в первый час после полуночи по Астане
                if now_almaty.hour == 0 and self._last_birthday_day != today:
                    self._last_birthday_day = today
                    await self._send_birthday_greetings(today)
            except Exception as e:
                logger.error(f"Error in birthday loop: {e}", exc_info=True)

    async def _send_birthday_greetings(self, today) -> None:
        """Отправить V2-поздравления именинникам за сегодня (Asia/Almaty).

        Дедупликация через birthday_greetings в PostgreSQL — рестарт
        бота внутри первого часа суток не создаёт дублей.
        """
        from config import NOTIFICATION_CHANNEL_ID
        from storage.birthday_greet_store import birthday_greet_store
        from storage.player_stats_store import player_stats_store
        from views.birthdays_view import days_until_birthday
        from services.social.birthday_congrats import (
            build_congrats_view, congrats_file,
        )

        for guild in self.guilds:
            if not guild_allowed(guild.id):
                continue
            try:
                stats_list = await player_stats_store.get_all(guild.id)
                greeted = 0
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
                    if member is None or member.bot:
                        continue
                    # Атомарная пометка до отправки: при ошибке отправки
                    # поздравление считается обработанным (без дублей).
                    if not await birthday_greet_store.try_mark(
                        guild.id, member.id, today
                    ):
                        continue
                    channel = guild.get_channel(NOTIFICATION_CHANNEL_ID)
                    if channel is None:
                        logger.warning(
                            f"Birthday greetings: notification channel "
                            f"{NOTIFICATION_CHANNEL_ID} not found in guild {guild.id}"
                        )
                        break
                    try:
                        await channel.send(
                            view=build_congrats_view(member),
                            file=congrats_file(),
                        )
                        greeted += 1
                    except Exception as e:
                        logger.error(
                            f"Birthday greeting failed for {member.id}: {e}",
                            exc_info=True,
                        )
                if greeted:
                    logger.info(
                        f"Birthday greetings sent: {greeted} in guild {guild.id}"
                    )
            except Exception as e:
                logger.error(f"Birthday greetings error for guild {guild.id}: {e}", exc_info=True)

    async def on_ready(self) -> None:
        import os
        commit = (
            os.getenv("RAILWAY_GIT_COMMIT_SHA")
            or os.getenv("GIT_COMMIT_SHA")
            or "unknown"
        )
        logger.info(
            "Бот запущен как %s (ID: %s), commit=%s",
            self.user, self.user.id, commit[:10],
        )

        # Sync commands with Discord
        try:
            synced = await self.tree.sync()
            logger.info(f"Синхронизировано {len(synced)} slash-команд")
        except Exception as e:
            logger.error(f"Ошибка синхронизации команд: {e}")

        # Restore org role panel if it exists
        from views.org_role_view import (
            load_state, OrgPanelView, org_banner_file,
        )
        state = load_state()
        if state.message_id > 0 and state.channel_id > 0 and state.guild_id > 0:
            try:
                guild = self.get_guild(state.guild_id)
                if guild:
                    channel = guild.get_channel(state.channel_id)
                    if channel:
                        try:
                            message = await channel.fetch_message(state.message_id)
                            view = OrgPanelView(guild=guild, state=state)
                            self.add_view(view, message_id=state.message_id)
                            # Пересобираем сообщение в новый CV2-вид
                            # (баннер + статусы + кнопки в одном сообщении).
                            try:
                                banner = org_banner_file()
                                await message.edit(
                                    view=view,
                                    attachments=(
                                        [banner] if banner else []
                                    ),
                                    allowed_mentions=(
                                        discord.AllowedMentions.none()
                                    ),
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Org panel re-render failed: %s", exc
                                )
                            logger.info("Org role panel restored")
                        except discord.NotFound:
                            logger.warning("Org role message not found, will be recreated")
            except Exception as e:
                logger.error(f"Error restoring org role view: {e}")

        # Classic layout: обновить карточки анонсов матчей и MM-матчей
        # (убраны блок «ИНФОРМАЦИЯ» и кнопки «Правила»). Одноразово
        # при старте, идемпотентно, результаты не затрагиваются.
        async def _refresh_match_cards() -> None:
            try:
                from services.match_announcer import refresh_announced_cards
                await refresh_announced_cards(self)
            except Exception as exc:
                logger.warning("Match card refresh failed: %s", exc)
            try:
                from views.mm_match_view import refresh_mm_cards
                await refresh_mm_cards(self)
            except Exception as exc:
                logger.warning("MM card refresh failed: %s", exc)

        self.loop.create_task(_refresh_match_cards())

        # Ультиматум 2.0: восстановить активные турниры после рестарта
        # (обновить карточки регистрации/сетки в каналах).
        try:
            _ult_cog = self.get_cog("UltimatumCog")
            if _ult_cog is not None and hasattr(_ult_cog, "cog_load_resume"):
                self.loop.create_task(_ult_cog.cog_load_resume())
        except Exception as exc:
            logger.warning("Ultimatum resume hook failed: %s", exc)

        # Debug: Log all registered commands
        commands = list(self.tree.walk_commands())
        logger.info(f"Зарегистрировано {len(commands)} команд:")
        for cmd in commands:
            cmd_type = cmd.type.__name__ if hasattr(cmd, 'type') else 'Group'
            logger.info(f"  - /{cmd.name} (type: {cmd_type})")

    async def close(self) -> None:
        """Graceful shutdown: остановить фоновые метрики."""
        try:
            from utils import perf_metrics

            await perf_metrics.stop()
        except Exception:
            pass
        await super().close()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Log when bot is added to a server; leave foreign guilds
        при заданном ALLOWED_GUILD_IDS (изоляция test/prod)."""
        from config import ALLOWED_GUILD_IDS
        from utils.logging import log_guild_join
        if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
            logger.warning(
                "Бот добавлен в неразрешённую гильдию %s — покидаю",
                guild.id,
            )
            try:
                await guild.leave()
            except Exception as exc:
                logger.error("Не удалось покинуть гильдию %s: %s", guild.id, exc)
            return
        await log_guild_join(self, guild)

    async def on_member_join(self, member: discord.Member) -> None:
        """Welcome-сообщение Components V2 для новых участников."""
        if not guild_allowed(member.guild.id):
            return
        from services.welcome_card import send_welcome_message
        await send_welcome_message(self, member)


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
