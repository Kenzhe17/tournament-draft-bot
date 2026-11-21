"""Ког ежедневной ротации роли «Доступ».

- Постоянная CV2-панель в #доступ-ротация (заявки, владельцы,
  активность, дата следующей ротации, история замен).
- Scheduler: ежедневно в 21:00 Asia/Almaty — одна замена
  наименее активного владельца на активного кандидата из очереди.
  Идемпотентно по access_rotation_runs (переживает рестарт).
- /admin access_status | access_rotate | access_exempt —
  только серверные администраторы/владелец бота.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    ACCESS_CHANNEL_NAMES,
    ACCESS_ROTATION_CHANNEL_ID,
    ACCESS_ROTATION_HOUR,
    ADMIN_USER_IDS,
    BOT_OWNER_ID,
)
from cogs.groups import admin_group, bind_commands
from services import access_rotation as rot
from services.banner import activity as act
from storage import access_store

logger = logging.getLogger(__name__)

ROTATION_CHECK_SECONDS = 60


def _is_server_admin(interaction: discord.Interaction) -> bool:
    """Только administrator/владелец/ADMIN_USER_IDS — без ORG-ролей."""
    user = interaction.user
    if user.id == BOT_OWNER_ID or user.id in ADMIN_USER_IDS:
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms is not None and getattr(perms, "administrator", False))


async def _require_admin(interaction: discord.Interaction) -> bool:
    if _is_server_admin(interaction):
        return True
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "Команда доступна только администраторам сервера.",
                ephemeral=True)
        else:
            await interaction.response.send_message(
                "Команда доступна только администраторам сервера.",
                ephemeral=True)
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Панель: канал и сообщение
# ---------------------------------------------------------------------------


async def resolve_panel_channel(guild: discord.Guild):
    """Канал панели: env ID → поиск по имени → создать."""
    cid = ACCESS_ROTATION_CHANNEL_ID
    if cid:
        ch = guild.get_channel(cid)
        if ch is None:
            try:
                ch = await guild.fetch_channel(cid)
            except Exception:
                ch = None
        if ch is not None:
            return ch
    wanted = {n.lower() for n in ACCESS_CHANNEL_NAMES}
    for ch in getattr(guild, "text_channels", []) or []:
        if str(getattr(ch, "name", "")).lower() in wanted:
            return ch
    try:
        return await guild.create_text_channel("доступ-ротация")
    except Exception as exc:
        logger.warning("access: cannot create panel channel: %s", exc)
        return None


async def ensure_panel(guild: discord.Guild) -> bool:
    """Создать/обновить единственную панель в канале (in-place)."""
    from views.access_panel import render_panel_view
    try:
        channel = await resolve_panel_channel(guild)
        if channel is None:
            return False
        ctx = await rot.panel_context(guild)
        view = render_panel_view(guild, ctx)
        state = await access_store.get_state(guild.id)
        mid = state.get("message_id")
        if mid:
            try:
                message = await channel.fetch_message(int(mid))
                await message.edit(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True
            except discord.NotFound:
                pass  # сообщение удалено — создаём новое
            except Exception:
                return False
        message = await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await access_store.set_panel(guild.id, channel.id, message.id)
        return True
    except Exception as exc:
        logger.warning("access: ensure_panel failed guild=%s: %s",
                       guild.id, exc)
        return False


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class AccessCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_ready: set[int] = set()

    async def cog_load(self) -> None:
        bind_commands(
            admin_group, self,
            ["access_status", "access_rotate", "access_exempt"],
        )
        self.rotation_loop.start()

    async def cog_unload(self) -> None:
        self.rotation_loop.cancel()

    # ---------------- scheduler ----------------

    def _rotation_due(self) -> bool:
        tz = act._almaty_tz()
        return datetime.now(tz).time() >= time(ACCESS_ROTATION_HOUR, 0)

    async def maybe_rotate(self, guild: discord.Guild) -> dict | None:
        if not self._rotation_due():
            return None
        result = await rot.run_rotation(guild, kind="auto")
        if result.get("status") in ("done", "already_today"):
            await self._after_rotation(guild, result)
        return result

    async def _after_rotation(self, guild, result) -> None:
        try:
            await ensure_panel(guild)
        except Exception:
            pass
        swapped = result.get("swapped")
        filled = result.get("filled") or []
        if not swapped and not filled:
            return
        try:
            from services import tournament_audit_service as audit_svc
            await audit_svc.record(
                guild_id=guild.id,
                channel_id=0,
                action=audit_svc.A.ACCESS_ROTATE,
                source="system",
                after={
                    "kind": "auto",
                    "swapped": swapped,
                    "filled": filled,
                    "day": result.get("day"),
                },
            )
        except Exception:
            pass

    @tasks.loop(seconds=ROTATION_CHECK_SECONDS)
    async def rotation_loop(self):
        for guild in self.bot.guilds:
            try:
                if guild.id not in self._panel_ready:
                    await ensure_panel(guild)
                    self._panel_ready.add(guild.id)
                await self.maybe_rotate(guild)
            except Exception:
                logger.exception("access rotation loop failed guild=%s",
                                 guild.id)

    @rotation_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()
        # Догоняем пропущенную ротацию после рестарта —
        # идемпотентность по access_rotation_runs.
        for guild in self.bot.guilds:
            try:
                if guild.id not in self._panel_ready:
                    await ensure_panel(guild)
                    self._panel_ready.add(guild.id)
                await self.maybe_rotate(guild)
            except Exception:
                logger.exception("access catch-up failed guild=%s",
                                 guild.id)

    # ---------------- /admin команды ----------------

    @admin_group.command(
        name="access_status",
        description="Ротация доступа: владельцы, активность, очередь",
    )
    @app_commands.guild_only()
    async def access_status(self, interaction: discord.Interaction) -> None:
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        ctx = await rot.panel_context(guild)
        scores = ctx["scores"]

        def _n(uid):
            m = guild.get_member(uid)
            return (getattr(m, "display_name", None) or m.name
                    if m is not None else f"@{uid}")

        lines = ["### РОТАЦИЯ ДОСТУПА", ""]
        lines.append(
            f"**Владельцев:** {len(ctx['holders'])}/{rot.ACCESS_MAX_HOLDERS}")
        for m in ctx["holders"][:rot.ACCESS_MAX_HOLDERS]:
            lines.append(f"- {_n(m.id)} — {scores.get(m.id, 0)} очк.")
        lines.append("")
        lines.append(f"**Очередь:** {ctx['queue_count']}")
        if ctx["queue"]:
            top = ctx["queue"][:10]
            lines.append("- " + ", ".join(
                f"{_n(u)} ({scores.get(u, 0)})" for u in top))
        lines.append("")
        nxt = ctx["next_rotation"]
        lines.append(
            f"**Следующая ротация:** "
            f"{discord.utils.format_dt(nxt, style='F')}")
        last = ctx.get("last")
        if last:
            lines.append(
                f"**Последняя замена:** {_n(last['old_user_id'])} → "
                f"{_n(last['new_user_id'])}")
        try:
            exemptions = await access_store.active_exemptions(guild.id)
            if exemptions:
                lines.append("")
                lines.append("**Защищены (exempt):** " + ", ".join(
                    _n(u) for u in exemptions))
        except Exception:
            pass
        await interaction.followup.send(
            "\n".join(lines)[:4000], ephemeral=True)

    @admin_group.command(
        name="access_rotate",
        description="Ручная ротация доступа (превью + подтверждение)",
    )
    @app_commands.guild_only()
    async def access_rotate(self, interaction: discord.Interaction) -> None:
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        preview = await rot.run_rotation(
            guild, kind="manual", dry_run=True)

        def _n(uid):
            m = guild.get_member(uid)
            return (getattr(m, "display_name", None) or m.name
                    if m is not None else f"@{uid}")

        lines = ["### ПРЕВЬЮ РОТАЦИИ", ""]
        if preview.get("filled"):
            lines.append("**Заполнение свободных мест:**")
            for f in preview["filled"]:
                lines.append(
                    f"- {_n(f['user_id'])} — {f['score']} очк.")
        sw = preview.get("swapped")
        if sw:
            lines.append("")
            lines.append(
                f"**Замена:** {_n(sw['old_user_id'])} "
                f"({sw['old_score']} очк.) → "
                f"{_n(sw['new_user_id'])} ({sw['new_score']} очк.)")
        reason = preview.get("skipped_reason")
        if reason:
            lines.append("")
            lines.append(f"**Без изменений:** `{reason}`")
        prot = preview.get("protected") or []
        if prot:
            lines.append("")
            lines.append("**Защищены от снятия:** " + ", ".join(
                f"{_n(p['user_id'])} ({p['reason']})" for p in prot))
        if not sw and not preview.get("filled"):
            await interaction.followup.send(
                "\n".join(lines)[:4000] + "\n\nНечего менять — "
                "ротация не требуется.", ephemeral=True)
            return
        view = _RotateConfirmView(guild_id=guild.id)
        await interaction.followup.send(
            "\n".join(lines)[:4000],
            view=view, ephemeral=True)

    @admin_group.command(
        name="access_exempt",
        description="Временная защита от ротации (hours<=0 — снять)",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        member="Участник", hours="Часов защиты (0 — снять защиту)")
    async def access_exempt(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        hours: int = 72,
    ) -> None:
        if not await _require_admin(interaction):
            return
        guild = interaction.guild
        if hours <= 0:
            try:
                removed = await access_store.clear_exemption(
                    guild.id, member.id)
            except Exception as exc:
                await interaction.response.send_message(
                    f"Ошибка БД: {exc}", ephemeral=True)
                return
            await interaction.response.send_message(
                f"Защита {'снята' if removed else 'не была установлена'} "
                f"для {member.display_name}.", ephemeral=True)
            return
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        try:
            await access_store.set_exemption(
                guild.id, member.id, until,
                reason="admin access_exempt",
                created_by=interaction.user.id)
        except Exception as exc:
            await interaction.response.send_message(
                f"Ошибка БД: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{member.display_name} защищён от ротации до "
            f"{discord.utils.format_dt(until, style='F')}.",
            ephemeral=True)
        try:
            from services import tournament_audit_service as audit_svc
            await audit_svc.record_interaction(
                interaction,
                action=audit_svc.A.ACCESS_EXEMPT,
                source="system",
                target_user_id=member.id,
                after={"until": until.isoformat(), "hours": hours},
            )
        except Exception:
            pass


class _RotateConfirmView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id

    @discord.ui.button(
        label="Подтвердить ротацию",
        style=discord.ButtonStyle.danger,
        custom_id="access_rotate_confirm",
    )
    async def confirm(
        self, interaction: discord.Interaction, _button
    ) -> None:
        if not _is_server_admin(interaction):
            await interaction.response.send_message(
                "Нет прав.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message(
                "Гильдия не совпадает.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await rot.run_rotation(guild, kind="manual",
                                        actor_id=interaction.user.id)
        # Панель и аудит — как у авто-ротации.
        cog = interaction.client.cogs.get("AccessCog")
        if cog is not None:
            await cog._after_rotation(guild, result)
        sw = result.get("swapped")
        if sw:
            await interaction.followup.send(
                f"Ротация выполнена: <@{sw['old_user_id']}> → "
                f"<@{sw['new_user_id']}>", ephemeral=True)
        else:
            await interaction.followup.send(
                f"Ротация не выполнена: "
                f"`{result.get('skipped_reason') or result.get('status')}`",
                ephemeral=True)

    @discord.ui.button(
        label="Отмена",
        style=discord.ButtonStyle.secondary,
        custom_id="access_rotate_cancel",
    )
    async def cancel(
        self, interaction: discord.Interaction, _button
    ) -> None:
        await interaction.response.send_message(
            "Отменено.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AccessCog(bot))
