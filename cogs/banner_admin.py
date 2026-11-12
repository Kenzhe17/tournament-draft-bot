"""Админ-команды /banner: preview, update, status, leaderboard, auto."""

from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from cogs.groups import banner_group, bind_commands
from services.banner import activity as act
from services.banner.publisher import (
    build_banner_for_day, publish_day_banner, yesterday_almaty,
)
from storage.banner_store import banner_store
from utils.permissions import is_admin

logger = logging.getLogger(__name__)


def _day_str(day) -> str:
    return day.strftime("%d.%m.%Y")


class BannerAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        bind_commands(
            banner_group,
            self,
            ["preview", "update", "status", "leaderboard", "auto"],
        )

    # ------------------------------------------------------------------

    @banner_group.command(name="preview", description="Предпросмотр баннера (только PNG, без публикации)")
    @is_admin()
    async def banner_preview(
        self, interaction: discord.Interaction,
        day: str | None = None,
    ) -> None:
        """day — необязательная дата ГГГГ-ММ-ДД; по умолчанию вчера."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        try:
            target = _parse_day(day) if day else yesterday_almaty()
        except ValueError:
            await interaction.followup.send(
                "Неверный формат даты. Используй ГГГГ-ММ-ДД.", ephemeral=True)
            return
        try:
            png, info = await build_banner_for_day(guild, target)
        except Exception as e:
            logger.exception("banner preview failed")
            await interaction.followup.send(
                f"Не удалось сгенерировать баннер: {e}", ephemeral=True)
            return
        winner = info["winner_name"] or "нет активности"
        await interaction.followup.send(
            content=f"Баннер за {_day_str(target)}. Участник дня: **{winner}**",
            file=discord.File(__import__("io").BytesIO(png), filename="banner.png"),
            ephemeral=True,
        )

    @banner_group.command(name="update", description="Вручную обновить Server Banner за вчера")
    @is_admin()
    async def banner_update(
        self, interaction: discord.Interaction, force: bool = False
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        day = yesterday_almaty()
        result = await publish_day_banner(guild, day, force=force)
        status = result.get("status")
        if status == "published":
            msg = (f"Баннер обновлён за {_day_str(day)}. "
                   f"Участник дня: **{result.get('winner_name') or '—'}**")
        elif status == "already_published":
            msg = (f"Баннер за {_day_str(day)} уже был опубликован. "
                   "Используй `force: true` для повторной публикации.")
        else:
            msg = f"Не удалось обновить баннер: {result.get('reason', status)}"
        await interaction.followup.send(msg, ephemeral=True)

    @banner_group.command(name="status", description="Статус автообновления баннера")
    @is_admin()
    async def banner_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        day = yesterday_almaty()
        upd = await banner_store.get_update(guild.id, day)
        auto = await banner_store.is_auto_enabled(guild.id)
        me = guild.me
        can_manage = bool(me and me.guild_permissions.manage_guild)
        # ближайшее обновление — сегодня/завтра 00:05 Almaty
        tz = act._almaty_tz()
        from datetime import datetime as _dt
        now = _dt.now(tz)
        nxt = now.replace(hour=0, minute=5, second=0, microsecond=0)
        if now.time() >= nxt.time():
            nxt += timedelta(days=1)
        upd_status = upd["status"] if upd else "нет данных"
        upd_detail = upd.get("detail") if upd else None

        lines = [
            f"**Автообновление:** {'включено' if auto else 'выключено'}",
            f"**Последняя публикация ({_day_str(day)}):** "
            + upd_status + (f" — {upd_detail}" if upd_detail else ""),
            f"**Участник дня:** {upd.get('winner_id') if upd else '—'}",
            f"**Manage Server у бота:** {'да' if can_manage else 'НЕТ'}",
            f"**Server Banner:** сервер поддерживает баннер",
            f"**Следующее обновление:** {nxt.strftime('%d.%m %H:%M')} (Almaty)",
        ]
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @banner_group.command(name="leaderboard", description="Топ-5 активности за день")
    @is_admin()
    async def banner_leaderboard(
        self, interaction: discord.Interaction,
        day: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        try:
            target = _parse_day(day) if day else yesterday_almaty()
        except ValueError:
            await interaction.followup.send(
                "Неверный формат даты. Используй ГГГГ-ММ-ДД.", ephemeral=True)
            return
        entries, winner = await act.compute_day(guild.id, target, banner_store)
        if not entries:
            await interaction.followup.send(
                f"За {_day_str(target)} активности не было.", ephemeral=True)
            return
        lines = [f"**Активность за {_day_str(target)}** (score = войс/5мин + 10×матчи)\n"]
        for i, e in enumerate(entries[:5], 1):
            member = guild.get_member(e.user_id)
            name = member.display_name if member else f"ID {e.user_id}"
            h = e.voice_seconds // 3600
            m = (e.voice_seconds % 3600) // 60
            vt = f"{h}ч {m}м" if h else f"{m}м"
            mark = " 👑" if winner and e.user_id == winner.user_id else ""
            lines.append(
                f"{i}. **{name}**{mark} — {e.score} очк. "
                f"({vt} войс, {e.matches} матч.)")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @banner_group.command(name="auto", description="Вкл/выкл автообновление баннера")
    @is_admin()
    async def banner_auto(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await banner_store.set_auto_enabled(interaction.guild.id, enabled)
        await interaction.followup.send(
            f"Автообновление баннера: {'включено' if enabled else 'выключено'}.",
            ephemeral=True)


def _parse_day(s: str):
    from datetime import date as _date
    y, m, d = (int(x) for x in s.strip().split("-"))
    return _date(y, m, d)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BannerAdminCog(bot))
