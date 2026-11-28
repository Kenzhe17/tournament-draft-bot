"""Информационный центр сервера: /info publish|update|status|validate.

Публикует 8 разделов (content/info/*.md + assets/info/*.png) как
Components V2 карточки с баннерами в один канал. ID сообщений и
версии хранятся в bot_settings (PostgreSQL) — обновление правит
существующие сообщения, дублей не создаёт.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import INFO_CHANNEL_ID, replace_emojis
from cogs.groups import bind_commands, info_group
from services.info_content import (
    BANNERS_DIR, SECTIONS, InfoSection, content_version,
    parse_blocks, section_by_key, validate as validate_content,
)
from services.info_renderer import build_info_view
from storage.info_store import info_store
from utils.design import compose
from utils.permissions import is_admin

logger = logging.getLogger(__name__)

_CHANNEL_NAME_CANDIDATES = ("info", "информация", "инфо", "📌・info")


class InfoCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        bind_commands(
            info_group, self, ["publish", "update", "status", "validate"]
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _resolve_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> discord.TextChannel | None:
        if channel is not None:
            return channel
        guild = interaction.guild
        cid = INFO_CHANNEL_ID or await info_store.get_channel_id(guild.id)
        if cid:
            ch = guild.get_channel(cid)
            if ch is None:
                try:
                    ch = await guild.fetch_channel(cid)
                except Exception:
                    ch = None
            if isinstance(ch, discord.TextChannel):
                return ch
        for ch in guild.text_channels:
            if ch.name.lower() in _CHANNEL_NAME_CANDIDATES:
                return ch
        return None

    async def _sync_section(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        section: InfoSection,
        *,
        force: bool = False,
    ) -> str:
        """Опубликовать/обновить один раздел. Вернуть статус-строку."""
        version = content_version(section)
        if version == "missing" or not parse_blocks(section):
            return f"{section.key}: нет контента — пропущен"

        banner_path = BANNERS_DIR / section.banner
        if not banner_path.exists():
            return f"{section.key}: нет баннера {section.banner} — пропущен"

        msgs = await info_store.get_messages(guild.id)
        rec = msgs.get(section.key) or {}
        mid = rec.get("message_id")

        view = build_info_view(section)

        # 1) Есть сохранённый message_id — пробуем отредактировать
        if mid and (force or rec.get("version") != version):
            try:
                msg = await channel.fetch_message(int(mid))
                await msg.edit(view=view)
                await info_store.set_message(
                    guild.id, section.key, int(mid), version
                )
                return f"{section.key}: обновлено (#{mid})"
            except discord.NotFound:
                logger.info("info %s: message %s deleted, resending",
                            section.key, mid)
            except Exception as exc:
                logger.warning("info %s edit failed: %s", section.key, exc)
        elif mid and not force:
            return f"{section.key}: актуально (#{mid})"

        # 2) Нет сообщения или edit не удался — отправляем новое
        file = discord.File(banner_path, filename=section.banner)
        msg = await channel.send(
            view=view,
            file=file,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await info_store.set_message(guild.id, section.key, msg.id, version)
        return f"{section.key}: опубликовано (#{msg.id})"

    async def _sync_all(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        force: bool = False,
        only: str | None = None,
    ) -> list[str]:
        results = []
        for section in SECTIONS:
            if only and section.key != only:
                continue
            try:
                results.append(
                    await self._sync_section(
                        guild, channel, section, force=force
                    )
                )
            except Exception as exc:
                logger.exception("info sync failed for %s", section.key)
                results.append(f"{section.key}: ОШИБКА — {exc}")
        return results

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    @info_group.command(
        name="publish",
        description="Опубликовать информационный центр в канал",
    )
    @app_commands.describe(
        channel="Канал для публикации (по умолчанию — сохранённый/по имени)"
    )
    @is_admin()
    async def publish(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        ch = await self._resolve_channel(interaction, channel)
        if ch is None:
            await interaction.followup.send(
                replace_emojis(
                    "Канал не найден. Укажите параметр channel или "
                    "задайте INFO_CHANNEL_ID."
                ),
                ephemeral=True,
            )
            return
        await info_store.set_channel_id(interaction.guild.id, ch.id)
        results = await self._sync_all(
            interaction.guild, ch, force=True
        )
        body = "\n".join(f"- {r}" for r in results)
        embed = discord.Embed(
            title="Информационный центр",
            description=(
                f"Канал: {ch.mention}\n\n{body}\n\n"
                "Сообщения публикуются один раз — при обновлении контента "
                "используйте `/info update`."
            ),
            color=discord.Color.from_rgb(170, 102, 255),
        )
        await interaction.followup.send(view=compose(embed), ephemeral=True)

    @info_group.command(
        name="update",
        description="Обновить сообщения инфо-центра после правки контента",
    )
    @app_commands.describe(section="Раздел (пусто = все)")
    @is_admin()
    async def update(
        self,
        interaction: discord.Interaction,
        section: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        key = (section or "").strip().lower() or None
        if key and section_by_key(key) is None:
            names = ", ".join(s.key for s in SECTIONS)
            await interaction.followup.send(
                replace_emojis(f"Неизвестный раздел. Доступные: {names}"),
                ephemeral=True,
            )
            return
        ch = await self._resolve_channel(interaction)
        if ch is None:
            await interaction.followup.send(
                replace_emojis(
                    "Канал инфо-центра не настроен — сначала `/info publish`."
                ),
                ephemeral=True,
            )
            return
        results = await self._sync_all(
            interaction.guild, ch, only=key
        )
        body = "\n".join(f"- {r}" for r in results)
        embed = discord.Embed(
            title="Обновление инфо-центра",
            description=f"Канал: {ch.mention}\n\n{body}",
            color=discord.Color.from_rgb(170, 102, 255),
        )
        await interaction.followup.send(view=compose(embed), ephemeral=True)

    @info_group.command(
        name="status",
        description="Статус информационного центра (канал, сообщения, версии)",
    )
    @is_admin()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        ch_id = await info_store.get_channel_id(guild.id)
        msgs = await info_store.get_messages(guild.id)
        ch = guild.get_channel(ch_id) if ch_id else None

        lines = [
            f"**Канал:** {ch.mention if ch else 'не настроен'}",
            "",
        ]
        for s in SECTIONS:
            rec = msgs.get(s.key) or {}
            mid = rec.get("message_id")
            ver = rec.get("version") or "—"
            cur = content_version(s)
            mark = "OK" if mid else "нет сообщения"
            stale = " (устарело)" if mid and ver != cur else ""
            ts = rec.get("updated_at")
            when = (
                time.strftime("%d.%m %H:%M", time.localtime(ts))
                if ts else "—"
            )
            lines.append(
                f"`{s.key:<12}` {mark}{stale} — msg `{mid or '—'}`, "
                f"v{ver}→v{cur}, {when}"
            )
        embed = discord.Embed(
            title="Информационный центр — статус",
            description="\n".join(lines),
            color=discord.Color.from_rgb(170, 102, 255),
        )
        await interaction.followup.send(view=compose(embed), ephemeral=True)

    @info_group.command(
        name="validate",
        description="Проверить контент и ассеты инфо-центра",
    )
    @is_admin()
    async def validate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        issues = validate_content()
        if issues:
            body = "\n".join(f"- {i}" for i in issues[:20])
            embed = discord.Embed(
                title="Валидация инфо-центра — проблемы",
                description=body,
                color=discord.Color.red(),
            )
        else:
            embed = discord.Embed(
                title="Валидация инфо-центра",
                description=(
                    f"Всё в порядке: {len(SECTIONS)} разделов, "
                    "все md-файлы и баннеры на месте, текст в лимитах."
                ),
                color=discord.Color.green(),
            )
        await interaction.followup.send(view=compose(embed), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InfoCog(bot))
