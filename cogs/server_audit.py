"""Owner-only, read-only /admin server_audit JSON export."""

from __future__ import annotations

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.groups import admin_group, bind_commands
from services.server_audit import (
    MAX_REPORT_BYTES,
    audit_allowed,
    build_server_snapshot,
    report_json_bytes,
)

logger = logging.getLogger(__name__)


class ServerAuditCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Shared root /admin group is registered in bot.py after extension loading.
        bind_commands(admin_group, self, ["server_audit"])

    @admin_group.command(
        name="server_audit",
        description="Экспорт структуры сервера в JSON (только владелец бота)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def server_audit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not audit_allowed(
            interaction.user.id,
            guild.id if guild is not None else None,
            owner_id=config.BOT_OWNER_ID,
            target_guild_id=config.GUILD_ID,
        ):
            await interaction.response.send_message(
                "Доступ запрещён. Команда доступна только владельцу бота "
                "на настроенном сервере r!z3. Проверьте BOT_OWNER_ID и GUILD_ID.",
                ephemeral=True,
            )
            return

        # All network reads happen only AFTER the in-code owner/guild check.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channels = await guild.fetch_channels()
            roles = await guild.fetch_roles()
            snapshot = build_server_snapshot(guild, channels, roles, config)
            raw = report_json_bytes(snapshot)
        except discord.Forbidden:
            logger.warning("Server audit forbidden by Discord (guild=%s)", guild.id)
            await interaction.followup.send(
                "Discord запретил чтение структуры сервера. "
                "Проверьте права и доступность каналов для бота.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception("Server audit HTTP read failed for guild=%s", guild.id)
            await interaction.followup.send(
                "Не удалось прочитать данные Discord. Попробуйте позже.",
                ephemeral=True,
            )
            return
        except Exception:
            logger.exception("Server audit failed for guild=%s", guild.id)
            await interaction.followup.send(
                "Не удалось собрать отчёт. Подробности доступны в логах бота.",
                ephemeral=True,
            )
            return

        if len(raw) > MAX_REPORT_BYTES:
            await interaction.followup.send(
                "Отчёт слишком большой для безопасной отправки вложением "
                "(более 7 МиБ). Данные не были опубликованы.",
                ephemeral=True,
            )
            return

        file = discord.File(
            io.BytesIO(raw),
            filename=f"riz3-server-audit-{guild.id}.json",
        )
        try:
            await interaction.followup.send(
                content=(
                    "Готово: снимок структуры сервера (только чтение). "
                    f"Каналов: **{len(channels)}**, ролей: **{len(roles)}**.\n"
                    "В отчёт не входят переписка, топики, токены и пароли. "
                    "Файл доступен только в этом приватном ответе; "
                    "не публикуйте его в общих каналах."
                ),
                file=file,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Server audit attachment delivery failed (guild=%s)", guild.id)
            await interaction.followup.send(
                "JSON создан, но Discord не смог доставить вложение. "
                "Попробуйте ещё раз или проверьте ограничение размера файлов.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerAuditCog(bot))
