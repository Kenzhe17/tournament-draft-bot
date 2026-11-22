"""Публичные публикации CASH CUP: сетка турнира + карточки матчей.

Два выделенных канала (config.CASHCUP_BRACKET_CHANNEL_ID /
CASHCUP_MATCH_CHANNEL_ID, 0 = выключено):
- «bracket» — одно сообщение с embeds всей сетки, обновляется на месте;
- «match:{id}» — отдельное сообщение на каждый открытый алгоритмом
  матч, обновляется на месте, хранится как история.

Источник истины — cashcup_tournaments (services/cashcup_store).
Публикации хранятся в cashcup_publications: уникальный индекс
(tournament_id, publication_key) + message_id + render-hash дают
идемпотентность, восстановление после рестарта и защиту от дублей.
Discord-вызовы выполняются ТОЛЬКО после commit состояния в PG
(через schedule_sync из CashCupCog._post и периодический retry-loop).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import discord
from discord.ext import commands

from config import CASHCUP_BRACKET_CHANNEL_ID, CASHCUP_MATCH_CHANNEL_ID
from services import cashcup_public_render as render
from services.cashcup_publication_store import cashcup_publication_store as pubstore
from services.cashcup_publication_store import utcnow
from services.cashcup_store import cashcup_store

logger = logging.getLogger(__name__)

RETRY_LOOP_SECONDS = 60
VERIFY_INTERVAL = timedelta(minutes=15)   # периодическая проверка живости
MAX_RETRY_BACKOFF = timedelta(minutes=10)
HISTORY_SCAN_LIMIT = 100

_OPEN_CUSTOM_ID = "ccpub:open"


class _OpenButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Открыть матч",
                         style=discord.ButtonStyle.primary,
                         custom_id=_OPEN_CUSTOM_ID)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _open_match(interaction)


async def _open_match(interaction: discord.Interaction) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
    except discord.InteractionResponded:
        pass
    pub = await pubstore.get_by_message(
        interaction.guild_id, interaction.message.id)
    key = str((pub or {}).get("publication_key") or "")
    if not pub or not key.startswith("match:"):
        await interaction.followup.send(
            "Эта кнопка не привязана к матчу CASH CUP.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return
    mid = key.split(":", 1)[1]
    cog = interaction.client.get_cog("CashCupCog")
    if cog is None:
        await interaction.followup.send(
            "CASH CUP временно недоступен — повторите позже.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return
    # Права проверяются заново внутри show_match_card/MatchCardView:
    # устаревшая кнопка не даёт доступа к чужому матчу.
    await cog.show_match_card(interaction, mid,
                              tid=pub["tournament_id"])


class PublicMatchOpenView(discord.ui.View):
    """Кнопка «Открыть матч» на публичной карточке.

    Один persistent custom_id на все сообщения: матч определяется
    через message_id → cashcup_publications. После рестарта достаточно
    одного bot.add_view — кнопки всех карточек продолжают работать.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(_OpenButton())


class CashCupPublicCog(commands.Cog):
    """Синхронизатор публичных публикаций CASH CUP."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}
        self._loop_task: asyncio.Task | None = None
        self._warned: set[str] = set()

    def cog_load(self) -> None:
        # Единый persistent view для всех карточек матчей.
        self.bot.add_view(PublicMatchOpenView())
        self._loop_task = asyncio.create_task(self._retry_loop())

    def cog_unload(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()

    # ---------- конфигурация ----------

    @staticmethod
    def _configured() -> bool:
        return CASHCUP_BRACKET_CHANNEL_ID > 0 and CASHCUP_MATCH_CHANNEL_ID > 0

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def _warn_once(self, tag: str, msg: str, *args) -> None:
        if tag not in self._warned:
            self._warned.add(tag)
            logger.warning(msg, *args)

    async def _resolve_channel(self, guild: discord.Guild,
                               channel_id: int, tag: str
                               ) -> discord.TextChannel | None:
        """Канал существует,属于 guild и бот может читать/писать."""
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden,
                    discord.HTTPException) as exc:
                self._warn_once(
                    f"{tag}:{channel_id}",
                    "CASHCUP public channel %s unavailable: %s",
                    channel_id, exc)
                return None
        if getattr(channel, "guild", None) is None \
                or channel.guild.id != guild.id:
            self._warn_once(f"{tag}:{channel_id}:guild",
                            "CASHCUP public channel %s belongs to "
                            "another guild — skipped", channel_id)
            return None
        me = guild.me
        if me is None:
            return None
        perms = channel.permissions_for(me)
        needed = ("view_channel", "send_messages", "embed_links",
                  "read_message_history")
        if not all(getattr(perms, name, False) for name in needed):
            self._warn_once(f"{tag}:{channel_id}:perms",
                            "CASHCUP public channel %s lacks required "
                            "permissions", channel_id)
            return None
        return channel

    # ---------- точка входа ----------

    async def _retry_loop(self) -> None:
        """Периодический проход: retry ошибок, верификация живости
        сообщений, восстановление после рестарта/удалений."""
        while True:
            await asyncio.sleep(RETRY_LOOP_SECONDS)
            for guild in list(self.bot.guilds):
                try:
                    await self.sync_guild(guild.id)
                except Exception:
                    logger.exception("CASHCUP public sync failed guild=%s",
                                     guild.id)

    async def sync_guild(self, guild_id: int) -> bool:
        """Полная идемпотентная синхронизация публикаций guild.

        Вызывается post-commit из CashCupCog._post и периодически.
        Никогда не изменяет состояние турнира и не создаёт матчи —
        только отражает cashcup_tournaments в Discord.
        """
        if not self._configured():
            return True
        state = await cashcup_store.latest(guild_id)
        if not state or not state.get("bracket") \
                or state.get("status") not in ("running", "completed"):
            return True
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        bracket_ch = await self._resolve_channel(
            guild, CASHCUP_BRACKET_CHANNEL_ID, "bracket")
        match_ch = await self._resolve_channel(
            guild, CASHCUP_MATCH_CHANNEL_ID, "match")
        if bracket_ch is None or match_ch is None:
            return False
        tid = state["tournament_id"]
        async with self._lock(guild_id):
            ok = await self._ensure(
                tid, guild_id, "bracket", bracket_ch,
                {"embeds": render.bracket_embeds(state),
                 "hash": render.bracket_hash(state)})
            for m in state["bracket"].get("matches", []):
                card = render.match_card(state, m)
                # Пинг капитанов — только для реально открытой пары.
                # Backfill завершённых матчей running-турнира не должен
                # уведомлять команды постфактум.
                content = card["content"] \
                    if m.get("status") == "ready" else None
                match_ok = await self._ensure(
                    tid, guild_id, f"match:{m['id']}", match_ch,
                    {"embed": card["embed"], "hash": card["hash"],
                     "content": content,
                     "ping_ids": card["ping_ids"]},
                    match_view=True)
                ok = ok and match_ok
            return ok

    # ---------- одна публикация ----------

    async def _ensure(self, tid: str, guild_id: int, key: str,
                      channel: discord.TextChannel,
                      desired: dict[str, Any], *,
                      match_view: bool = False) -> bool:
        """Создать/обновить/проверить одну публикацию. Идемпотентно."""
        try:
            row = await pubstore.ensure_row(tid, guild_id, key)
        except Exception:
            logger.exception("CASHCUP pub row failed %s/%s", tid, key)
            return False
        now = utcnow()
        same_hash = row.get("last_render_hash") == desired["hash"]
        due = row.get("next_attempt_at") is None \
            or row["next_attempt_at"] <= now
        # Здоровое сообщение без изменений и не due — пропускаем
        # без Discord-запросов (основная масса sync-вызовов).
        if row.get("message_id") and same_hash and not due:
            return True
        # Error-строка в backoff — ждём next_attempt_at независимо
        # от хэша (не долбим Discord при 429/5xx).
        if row.get("status") == "error" and not due:
            return True
        # Строка в backoff после ошибки и сообщения ещё нет —
        # не шлём раньше next_attempt_at.
        if not row.get("message_id") and not due:
            return True

        msg = None
        adopted = False  # сообщение найдено по marker, id ещё не в БД
        if row.get("message_id"):
            try:
                msg = await channel.fetch_message(row["message_id"])
            except discord.NotFound:
                msg = None  # удалено вручную — восстановим ниже
            except discord.HTTPException as exc:
                logger.warning("CASHCUP pub fetch failed %s/%s: %s",
                               tid, key, exc)
                await pubstore.update(row["id"], bump_retry=True)
                return False

        if msg is None:
            # При потерянном ответе/удалении — ищем ранее
            # опубликованное сообщение по маркеру в footer.
            # Ошибка чтения истории ≠ «публикации нет»: без
            # надёжного подтверждения отсутствие send запрещён,
            # иначе получим дубль карточки.
            try:
                msg = await self._find_by_marker(channel, tid, key)
            except Exception as exc:
                logger.warning("CASHCUP pub history scan failed "
                               "%s/%s: %s", tid, key, exc)
                await pubstore.update(row["id"], bump_retry=True)
                return False
            if msg is not None:
                adopted = True

        if msg is None:
            return await self._send_new(
                row, channel, key, desired, match_view, tid)

        if same_hash and due:
            # Верификация: сообщение живо, контент не изменился.
            # При adoption фиксируем фактический message_id.
            await pubstore.update(
                row["id"], status="ok", retry_count=0,
                next_attempt_at=now + VERIFY_INTERVAL,
                **({"channel_id": channel.id,
                    "message_id": msg.id} if adopted else {}))
            return True
        if same_hash and not due:
            # Backoff/error-строка с живым сообщением — не дёргаем
            # Discord чаще next_attempt_at.
            return True
        return await self._edit_existing(
            row, msg, desired, match_view, tid, guild_id,
            adopted=adopted)

    async def _send_new(self, row: dict, channel: discord.TextChannel,
                        key: str, desired: dict[str, Any],
                        match_view: bool, tid: str) -> bool:
        """Публикация новой карточки. Пинг капитанов — только один
        раз за жизнь публикации (row['pinged'])."""
        ping = bool(desired.get("content") and desired.get("ping_ids")
                    and not row.get("pinged"))
        kwargs: dict[str, Any] = {}
        if "embeds" in desired:
            kwargs["embeds"] = desired["embeds"]
        else:
            kwargs["embed"] = desired["embed"]
        if match_view:
            kwargs["view"] = PublicMatchOpenView()
        if ping:
            kwargs["content"] = desired["content"]
            kwargs["allowed_mentions"] = discord.AllowedMentions(
                users=[discord.Object(id=u)
                       for u in desired["ping_ids"]])
        else:
            kwargs["allowed_mentions"] = discord.AllowedMentions.none()
        try:
            msg = await channel.send(**kwargs)
        except Exception as exc:
            # Включая потерянный ответ после фактической отправки:
            # сообщение-сирота будет найдено по marker на следующем
            # sync — send повторять вслепую нельзя.
            logger.warning("CASHCUP pub send failed %s/%s: %s",
                           tid, key, exc)
            await pubstore.update(row["id"], bump_retry=True)
            return False
        try:
            await pubstore.update(
                row["id"], channel_id=channel.id, message_id=msg.id,
                last_render_hash=desired["hash"], status="ok",
                pinged=bool(row.get("pinged") or ping),
                retry_count=0,
                next_attempt_at=utcnow() + VERIFY_INTERVAL)
        except Exception:
            # Сообщение опубликовано, но id не сохранён — удаляем
            # сироту, иначе следующий sync создаст дубль.
            try:
                await msg.delete()
            except discord.HTTPException:
                logger.debug("CASHCUP pub orphan %s not deleted",
                             getattr(msg, "id", "?"))
            logger.exception("CASHCUP pub id save failed %s/%s", tid, key)
            return False
        return True

    async def _edit_existing(self, row: dict, msg: discord.Message,
                             desired: dict[str, Any], match_view: bool,
                             tid: str, guild_id: int, *,
                             adopted: bool = False) -> bool:
        """Обновление существующей публикации на месте.

        При adoption (сообщение найдено по marker, id не был в БД)
        сохраняем фактический message_id/channel_id. Если у
        принятого сообщения уже был ping-контент (send прошёл,
        а запись pinged упала) — фиксируем pinged=True, чтобы
        пересоздание не пинговало повторно."""
        had_ping_content = bool(getattr(msg, "content", "") or "")
        kwargs: dict[str, Any] = {"content": "",
                                  "allowed_mentions":
                                  discord.AllowedMentions.none()}
        if "embeds" in desired:
            kwargs["embeds"] = desired["embeds"]
        else:
            kwargs["embed"] = desired["embed"]
            if match_view:
                kwargs["view"] = PublicMatchOpenView()
        try:
            await msg.edit(**kwargs)
        except discord.NotFound:
            # Удалили между fetch и edit — пересоздаём без пинга.
            return await self._send_new(
                {**row, "pinged": True}, msg.channel,
                row["publication_key"], desired, match_view, tid)
        except discord.HTTPException as exc:
            logger.warning("CASHCUP pub edit failed %s/%s: %s",
                           tid, row["publication_key"], exc)
            await pubstore.update(row["id"], bump_retry=True)
            return False
        try:
            await pubstore.update(
                row["id"], last_render_hash=desired["hash"],
                status="ok", retry_count=0,
                next_attempt_at=utcnow() + VERIFY_INTERVAL,
                pinged=(True if had_ping_content
                        else row.get("pinged")),
                **({"channel_id": msg.channel.id,
                    "message_id": msg.id} if adopted else {}))
        except Exception:
            logger.exception("CASHCUP pub hash save failed %s/%s",
                             tid, row["publication_key"])
            return False
        return True

    async def _find_by_marker(self, channel: discord.TextChannel,
                              tid: str, key: str
                              ) -> discord.Message | None:
        """Поиск ранее отправленного сообщения по маркеру
        '{tid}:{key}' в footer первого embed — восстановление после
        потерянного ответа Discord без создания дубля.

        При HTTP-ошибке истории пробрасывает исключение — «не
        удалось проверить» не равно «публикации нет»."""
        marker = f"{tid}:{key}"
        bot_id = getattr(getattr(self.bot, "user", None), "id", 0)
        async for msg in channel.history(limit=HISTORY_SCAN_LIMIT):
            author = getattr(msg, "author", None)
            if bot_id and getattr(author, "id", None) != bot_id:
                continue
            for emb in msg.embeds:
                if emb.footer and emb.footer.text \
                        and marker in emb.footer.text:
                    return msg
        return None


def schedule_sync(bot: commands.Bot, guild_id: int) -> None:
    """Post-commit триггер из CashCupCog: синхронизация публичных
    каналов в фоне, не блокируя ответ пользователю."""
    try:
        cog = bot.get_cog("CashCupPublicCog")
        if cog is None:
            return
        asyncio.get_running_loop().create_task(
            cog.sync_guild(guild_id))
    except Exception:
        logger.debug("CASHCUP public sync schedule failed",
                     exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CashCupPublicCog(bot))
