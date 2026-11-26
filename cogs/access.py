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

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    ACCESS_ROTATION_CHANNEL_ID,
    ACCESS_ROTATION_HOUR,
    ADMIN_USER_IDS,
    BOT_OWNER_ID,
)
from cogs.groups import admin_group, bind_commands
from services import access_rotation as rot
from services.banner import activity as act
from storage import access_store
from utils.access_banner import banner_file, edit_attachments

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
    """Канал панели — делегировано сервису: только закреплённый
    ACCESS_ROTATION_CHANNEL_ID, без автосоздания и без выбора
    дубликата по имени."""
    return await rot.resolve_panel_channel(guild)


_PANEL_LOCKS: dict[int, asyncio.Lock] = {}


def _panel_lock(guild_id: int) -> asyncio.Lock:
    return _PANEL_LOCKS.setdefault(guild_id, asyncio.Lock())


async def _edit_panel_message(message, view) -> None:
    edit_kwargs = {}
    att = edit_attachments(message)
    if att is not None:
        edit_kwargs["attachments"] = att
    await message.edit(
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
        **edit_kwargs,
    )


async def _retry_429(coro_factory):
    """Один повтор при Discord 429 с ПОЛНЫМ retry_after.

    discord.py сам ждёт retry_after внутри HTTP-слоя; если
    исключение всё же дошло сюда — ждём честное время,
    а не обрезаем до 5 секунд (преждевременный повтор только
    усугубляет rate-limit).
    """
    try:
        return await coro_factory()
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) != 429:
            raise
        delay = float(getattr(exc, "retry_after", 1.0) or 1.0)
        await asyncio.sleep(delay + 0.25)
        return await coro_factory()


async def _ensure_panel_locked(guild: discord.Guild) -> bool:
    """Тело ensure_panel под per-guild asyncio.Lock + PG mutex."""
    from views.access_panel import render_panel_view
    try:
        channel = await resolve_panel_channel(guild)
        if channel is None:
            return False
        ctx = await rot.panel_context(guild)
        view = render_panel_view(guild, ctx)
        state = await access_store.get_state(guild.id)
        stored_cid = state.get("channel_id")
        mid = state.get("message_id")
        resync = False
        if stored_cid and int(stored_cid) != int(channel.id):
            # В access_state старый channel_id дублирующего канала:
            # message_id оттуда НЕ переносим — ищем панель внутри
            # закреплённого канала и сохраняем правильную пару.
            logger.warning(
                "access: stored panel channel %s != pinned %s "
                "guild=%s — syncing state", stored_cid, channel.id,
                guild.id)
            mid = None
            resync = True
        message = None
        if mid:
            try:
                message = await _retry_429(
                    lambda: channel.fetch_message(int(mid)))
            except discord.NotFound:
                message = None
            except discord.Forbidden:
                logger.error(
                    "access: cannot read panel message in pinned "
                    "channel %s", channel.id)
                return False
            except discord.HTTPException as exc:
                logger.warning("access: panel fetch failed: %s", exc)
                return False
        if message is not None:
            try:
                await _retry_429(lambda: _edit_panel_message(
                    message, view))
            except discord.NotFound:
                message = None  # удалено между fetch и edit — ищем ниже
            except discord.Forbidden:
                logger.error(
                    "access: cannot edit panel message %s",
                    message.id)
                return False
            except discord.HTTPException as exc:
                logger.warning("access: panel edit failed: %s", exc)
                return False
            else:
                if resync or state.get("channel_id") != channel.id:
                    try:
                        await access_store.set_panel(
                            guild.id, channel.id, message.id)
                    except Exception as exc:
                        logger.warning(
                            "access: set_panel sync failed: %s", exc)
                return True
        # Сообщения из state нет (или канал сменился) — сначала
        # ищем уже опубликованную панель в закреплённом канале,
        # чтобы не отправить второе сообщение после сбоя
        # сохранения message_id.
        found, search_complete = await rot.find_panel_message(channel)
        if found is None and not search_complete:
            # Поиск панели не удался/неполный — НЕЛЬЗЯ отправлять
            # новую: рискуем создать дубликат действующей панели.
            logger.warning(
                "access: panel search incomplete channel=%s — "
                "skipping send", channel.id)
            return False
        if found is not None:
            try:
                await _retry_429(lambda: _edit_panel_message(
                    found, view))
            except discord.HTTPException as exc:
                logger.warning(
                    "access: found-panel edit failed: %s", exc)
                return False
            try:
                await access_store.set_panel(
                    guild.id, channel.id, found.id)
            except Exception as exc:
                logger.warning(
                    "access: set_panel after find failed: %s", exc)
                return False
            return True
        # Панели в закреплённом канале нет — единственное
        # разрешённое действие: ОДНО сообщение панели. Каналы
        # не создаются никогда.
        try:
            message = await _retry_429(lambda: channel.send(
                view=view,
                file=banner_file(),
                allowed_mentions=discord.AllowedMentions.none(),
            ))
        except discord.Forbidden:
            logger.error(
                "access: no permission to send panel in pinned "
                "channel %s", channel.id)
            return False
        except discord.HTTPException as exc:
            logger.warning("access: panel send failed: %s", exc)
            return False
        try:
            await access_store.set_panel(
                guild.id, channel.id, message.id)
        except Exception as exc:
            # Сообщение уже опубликовано — следующий запуск найдёт
            # его через find_panel_message и не отправит второе.
            logger.warning(
                "access: set_panel failed after send: %s", exc)
            return False
        return True
    except Exception as exc:
        logger.warning("access: ensure_panel failed guild=%s: %s",
                       guild.id, exc)
        return False


async def ensure_panel(guild: discord.Guild) -> bool:
    """Создать/обновить единственную панель в закреплённом канале.

    Двойная блокировка: asyncio.Lock на guild_id (внутри
    процесса) + pg advisory lock через access_store.panel_mutex
    (между процессами). Никаких новых каналов.
    """
    if guild is None:
        return False
    async with _panel_lock(guild.id):
        mutex = getattr(access_store, "panel_mutex", None)
        if mutex is None:
            return await _ensure_panel_locked(guild)
        try:
            async with mutex(guild.id) as acquired:
                if not acquired:
                    logger.info(
                        "access: panel update skipped guild=%s "
                        "(mutex held by another process)", guild.id)
                    return False
                return await _ensure_panel_locked(guild)
        except Exception as exc:
            logger.warning(
                "access: panel mutex failed guild=%s: %s",
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
