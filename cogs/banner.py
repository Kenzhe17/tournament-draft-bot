"""Ког ежедневного баннера сервера.

- Сканер голосовых каналов (каждые 60 сек): считает присутствие
  реальных участников в общем войсе, накапливает до 6 ч/сутки,
  пишет в banner_voice_daily (PostgreSQL).
- Планировщик: каждый день в 00:05 (Asia/Almaty) публикует
  Server Banner за вчерашний день. Пропущенные дни догоняются
  после рестарта. Идемпотентно через banner_updates.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

import discord
from discord.ext import commands, tasks

from services.banner import activity as act
from services.banner.publisher import publish_day_banner, yesterday_almaty
from storage.banner_store import banner_store

logger = logging.getLogger(__name__)

SCAN_SECONDS = 60
DAILY_CHECK_SECONDS = 300


class BannerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, user_id, day) -> уже зачтённые секунды сегодня
        self._credited: dict[tuple[int, int, object], int] = {}

    async def cog_load(self) -> None:
        self.voice_scan_loop.start()
        self.daily_banner_loop.start()

    async def cog_unload(self) -> None:
        self.voice_scan_loop.cancel()
        self.daily_banner_loop.cancel()

    # ------------------------------------------------------------------
    # Voice activity scanner
    # ------------------------------------------------------------------

    def _eligible_members(self, channel) -> list[discord.Member]:
        """Реальные участники, для которых канал 'живой'."""
        humans = []
        for m in channel.members:
            if m.bot:
                continue
            voice = getattr(m, "voice", None)
            if voice is None:
                continue
            if getattr(voice, "self_deaf", False) or getattr(voice, "deaf", False):
                continue
            humans.append(m)
        return humans

    async def scan_voice_once(self) -> None:
        """Один проход по всем голосовым каналам всех гильдий."""
        now_utc = datetime.now(timezone.utc)
        day = act.today_almaty(now_utc)
        elapsed = SCAN_SECONDS

        for guild in self.bot.guilds:
            try:
                await self._scan_guild(guild, day, elapsed)
            except Exception:
                logger.exception("banner voice scan failed guild=%s", guild.id)

    async def _scan_guild(self, guild, day, elapsed: int) -> None:
        to_credit: list[tuple[int, int, int]] = []  # (uid, delta, )
        for channel in guild.voice_channels:
            if getattr(channel, "afk", False):
                continue
            humans = self._eligible_members(channel)
            if len(humans) < 2:
                continue
            for member in humans:
                key = (guild.id, member.id, day)
                prev = self._credited.get(key)
                if prev is None:
                    try:
                        prev = await banner_store.get_voice_seconds(
                            guild.id, member.id, day)
                    except Exception:
                        logger.exception("banner voice db read failed")
                        prev = 0
                    self._credited[key] = prev
                new = min(prev + elapsed, act.MAX_VOICE_SECONDS)
                delta = new - prev
                if delta > 0:
                    self._credited[key] = new
                    to_credit.append((member.id, delta))
        for uid, delta in to_credit:
            try:
                await banner_store.add_voice_seconds(guild.id, uid, day, delta)
            except Exception:
                logger.exception("banner voice db write failed")
        # чистим ключи других дней, чтобы dict не рос
        if len(self._credited) > 5000:
            self._credited = {
                k: v for k, v in self._credited.items() if k[2] == day
            }

    @tasks.loop(seconds=SCAN_SECONDS)
    async def voice_scan_loop(self):
        await self.scan_voice_once()

    @voice_scan_loop.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Daily banner publisher
    # ------------------------------------------------------------------

    def _should_publish_now(self) -> bool:
        """Сейчас по Asia/Almaty >= 00:05."""
        tz = act._almaty_tz()
        now = datetime.now(tz)
        return now.time() >= time(0, 5)

    async def maybe_publish(self, guild: discord.Guild) -> dict | None:
        """Опубликовать баннер за вчера, если ещё не опубликован."""
        day = yesterday_almaty()
        existing = await banner_store.get_update(guild.id, day)
        if existing and existing.get("status") == "published":
            return None
        if not await banner_store.is_auto_enabled(guild.id):
            return {"status": "auto_disabled"}
        return await publish_day_banner(guild, day)

    @tasks.loop(seconds=DAILY_CHECK_SECONDS)
    async def daily_banner_loop(self):
        if not self._should_publish_now():
            return
        for guild in self.bot.guilds:
            try:
                result = await self.maybe_publish(guild)
                if result:
                    logger.info("banner daily guild=%s -> %s",
                                guild.id, result.get("status"))
            except Exception:
                logger.exception("banner daily failed guild=%s", guild.id)

    @daily_banner_loop.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_ready()
        # Догоняем пропущенное сразу после рестарта
        if self._should_publish_now():
            for guild in self.bot.guilds:
                try:
                    await self.maybe_publish(guild)
                except Exception:
                    logger.exception("banner catch-up failed guild=%s",
                                     guild.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BannerCog(bot))
