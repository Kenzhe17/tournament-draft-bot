"""Постоянная CV2-панель ротации роли «Доступ».

Одна панель в канале #доступ-ротация: владельцы + активность,
очередь, дата следующей ротации, последняя замена, кнопки
«Подать заявку» / «Отозвать заявку». Контент пересобирается
сервисом (services/access_rotation.refresh_panel) — view
persistent, custom_id статичны.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from config import ACCESS_ROLE_ID
from storage import access_store
from services import access_rotation as rot
from utils.access_banner import (
    MEDIA as BANNER_MEDIA,
    available as banner_available,
    edit_attachments as banner_edit_attachments,
)

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()


def _name(guild: discord.Guild | None, user_id: int) -> str:
    if guild is not None:
        m = guild.get_member(user_id)
        if m is not None:
            return getattr(m, "display_name", None) or m.name
    return f"@{user_id}"


def render_panel_view(
    guild: discord.Guild | None,
    ctx: dict[str, Any],
) -> discord.ui.LayoutView:
    """Собрать LayoutView панели из контекста panel_context."""
    scores = ctx.get("scores") or {}
    holders = ctx.get("holders") or []
    lines = ["## РОТАЦИЯ ДОСТУПА", ""]

    lines.append(f"**Владельцев доступа:** {len(holders)}/{rot.ACCESS_MAX_HOLDERS}")
    if holders:
        for m in holders[: rot.ACCESS_MAX_HOLDERS]:
            lines.append(
                f"- {_name(guild, m.id)} — **{scores.get(m.id, 0)}** очк.")
    else:
        lines.append("- пока никого")

    lines.append("")
    lines.append(f"**В очереди на доступ:** {ctx.get('queue_count', 0)}")
    nxt = ctx.get("next_rotation")
    if nxt is not None:
        lines.append(
            f"**Следующая ротация:** {discord.utils.format_dt(nxt, style='F')}")

    last = ctx.get("last")
    if last:
        old_s = last.get("old_score")
        new_s = last.get("new_score")
        lines.append(
            "**Последняя замена:** "
            f"{_name(guild, last['old_user_id']) if last.get('old_user_id') else '—'}"
            f" ({old_s if old_s is not None else '—'}) → "
            f"{_name(guild, last['new_user_id']) if last.get('new_user_id') else '—'}"
            f" ({new_s if new_s is not None else '—'})"
        )
    else:
        lines.append("**Последняя замена:** —")

    lines.append("")
    lines.append(
        "-# Ротация ежедневно в 21:00 (Asia/Almaty): самый неактивный "
        "владелец заменяется активным кандидатом из очереди. "
        "Активность — голос и подтверждённые матчи за 7 дней.")

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=discord.Colour.blurple())
    if banner_available():
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=BANNER_MEDIA)
            )
        )
        container.add_item(discord.ui.Separator(
            spacing=discord.SeparatorSpacing.large))
    container.add_item(discord.ui.TextDisplay(content="\n".join(lines)[:4000]))
    row = discord.ui.ActionRow()
    row.add_item(_JoinButton())
    row.add_item(_LeaveButton())
    row.add_item(_GuideButton())
    row.add_item(_OrgPanelButton())
    container.add_item(row)
    view.add_item(container)
    return view


class AccessPanelView(discord.ui.View):
    """Persistent View для dispatch кнопок панели по custom_id.

    Сама панель — LayoutView из render_panel_view; этот View
    регистрируется в bot.add_view и связывает custom_id
    ``access:join``/``access:leave`` с обработчиками после
    рестарта (содержимое сообщения не затрагивается).
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(_JoinButton())
        self.add_item(_LeaveButton())
        self.add_item(_GuideButton())
        self.add_item(_OrgPanelButton())


class _GuideButton(discord.ui.Button):
    """«Как стать организатором» — приватная CV2-инструкция."""

    def __init__(self) -> None:
        super().__init__(
            label="Как стать организатором",
            style=discord.ButtonStyle.primary,
            custom_id="access:guide",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from services.access_academy import build_guide_view
        await interaction.response.send_message(
            view=build_guide_view(),
            ephemeral=True,
            allowed_mentions=_NO_MENTIONS,
        )


class _OrgPanelButton(discord.ui.Button):
    """«Панель организатора» — ссылка на сообщение ORG-панели."""

    def __init__(self) -> None:
        super().__init__(
            label="Панель организатора",
            style=discord.ButtonStyle.secondary,
            custom_id="access:orgpanel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from services.access_academy import org_panel_url
        guild = interaction.guild
        url = org_panel_url(guild.id) if guild is not None else None
        if url:
            await interaction.response.send_message(
                f"Панель организатора: {url}", ephemeral=True,
                allowed_mentions=_NO_MENTIONS)
        else:
            await interaction.response.send_message(
                "Панель организатора пока не настроена — "
                "обратитесь к администратору.",
                ephemeral=True, allowed_mentions=_NO_MENTIONS)


class _JoinButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Подать заявку",
            style=discord.ButtonStyle.success,
            custom_id="access:join",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Панель работает только на сервере.", ephemeral=True)
            return
        uid = interaction.user.id
        if ACCESS_ROLE_ID > 0:
            member = guild.get_member(uid)
            if member is not None and ACCESS_ROLE_ID in {
                    r.id for r in member.roles}:
                await interaction.response.send_message(
                    "У вас уже есть доступ — заявка не нужна.",
                    ephemeral=True)
                return
        try:
            await access_store.queue_join(guild.id, uid)
        except Exception as exc:
            logger.warning("access join failed: %s", exc)
            await interaction.response.send_message(
                "Не удалось сохранить заявку. Попробуйте позже.",
                ephemeral=True)
            return
        try:
            from services import tournament_audit_service as audit_svc
            await audit_svc.record_interaction(
                interaction,
                action=audit_svc.A.ACCESS_JOIN,
                source="system",
                target_user_id=uid,
            )
        except Exception:
            pass
        await interaction.response.send_message(
            "Заявка на доступ подана. Вы попадёте в очередь — "
            "о ротации сообщит панель.",
            ephemeral=True,
        )
        try:
            await rot.refresh_panel(guild)
        except Exception:
            pass


class _LeaveButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Отозвать заявку",
            style=discord.ButtonStyle.secondary,
            custom_id="access:leave",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Панель работает только на сервере.", ephemeral=True)
            return
        try:
            removed = await access_store.queue_leave(
                guild.id, interaction.user.id)
        except Exception as exc:
            logger.warning("access leave failed: %s", exc)
            await interaction.response.send_message(
                "Не удалось отозвать заявку. Попробуйте позже.",
                ephemeral=True)
            return
        if removed:
            try:
                from services import tournament_audit_service as audit_svc
                await audit_svc.record_interaction(
                    interaction,
                    action=audit_svc.A.ACCESS_LEAVE,
                    source="system",
                    target_user_id=interaction.user.id,
                )
            except Exception:
                pass
        await interaction.response.send_message(
            "Заявка отозвана." if removed else "Активной заявки нет.",
            ephemeral=True,
        )
        try:
            await rot.refresh_panel(guild)
        except Exception:
            pass
