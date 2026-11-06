"""Cog: Arbiter — esports referee & tournament ops (порт Arbiter)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from arbiter.enums import OrgRole
from arbiter.services.org_service import OrgError, org_service
from arbiter.db import init_arbiter_schema
from utils.design.embeds import DesignEmbed

logger = logging.getLogger(__name__)

_ROLE_CHOICES = [
    app_commands.Choice(name="Admin", value="ADMIN"),
    app_commands.Choice(name="Manager", value="MANAGER"),
    app_commands.Choice(name="Head Ref", value="HEAD_REF"),
    app_commands.Choice(name="Referee", value="REFEREE"),
    app_commands.Choice(name="Player", value="PLAYER"),
]


def _err_embed(message: str) -> DesignEmbed:
    return DesignEmbed(category="error", title="Ошибка", description=message)


class ArbiterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _ensure_schema(self) -> None:
        try:
            await init_arbiter_schema()
        except Exception as e:
            logger.error("Arbiter schema init failed: %s", e)

    arbiter = app_commands.Group(name="arb", description="Arbiter — организация, матчи и турниры")

    # ---------- org setup ----------

    @arbiter.command(name="setup", description="Создать/инициализировать организацию на сервере")
    async def setup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        if not interaction.guild:
            await interaction.followup.send(embed=_err_embed("Команда только на сервере."), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(embed=_err_embed("Требуется участник сервера."), ephemeral=True)
            return
        try:
            org = await org_service.get_or_create_org(interaction.guild, interaction.user)
            member = await org_service.get_member_role_for_user(org.id, interaction.user.id)
            is_owner = member == OrgRole.OWNER
            embed = DesignEmbed(category="success", title="Организация готова",
                                description=f"**{org.name}** инициализирована.")
            embed.add_kv("Ваша роль", member.value if member else "—")
            if is_owner:
                embed.add_kv("Далее", "`/arb member add` — добавить судей и админов")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.exception("arb setup failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    # ---------- members ----------

    @arbiter.command(name="member", description="Управление членами организации")
    @app_commands.describe(action="Действие", user="Пользователь", role="Роль")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Добавить", value="add"),
            app_commands.Choice(name="Удалить", value="remove"),
            app_commands.Choice(name="Список", value="list"),
        ]
    )
    async def member(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.User | None = None,
        role: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена. Используйте /arb setup.")

            if action.value == "list":
                members = await org_service.list_members(org.id)
                embed = DesignEmbed(category="info", title="Члены организации",
                                    description=f"**{org.name}**")
                for m, p in members[:20]:
                    embed.add_kv(f"{p.display_name or p.discord_user_id}", m.role.value)
                if not members:
                    embed.description = "Нет членов."
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # add / remove требуют ADMIN+
            profile = await org_service.get_or_create_profile(interaction.user)
            actor = await org_service.get_member(org.id, profile.id)
            if not actor or actor.role not in (OrgRole.OWNER, OrgRole.ADMIN):
                raise OrgError("Только Owner/Admin могут управлять членами.")

            if not user:
                raise OrgError("Укажите пользователя.")

            if action.value == "add":
                if not role:
                    raise OrgError("Укажите роль.")
                await org_service.add_member(org.id, user.id, OrgRole(role.value),
                                             actor_profile_id=profile.id)
                await interaction.followup.send(
                    embed=DesignEmbed(category="success", title="Участник добавлен",
                                      description=f"{user.mention} → **{role.value}**"),
                    ephemeral=True,
                )
            else:
                ok = await org_service.remove_member(org.id, user.id,
                                                     actor_profile_id=profile.id)
                if not ok:
                    raise OrgError("Пользователь не найден в организации.")
                await interaction.followup.send(
                    embed=DesignEmbed(category="success", title="Участник удалён",
                                      description=f"{user.mention} удалён из организации."),
                    ephemeral=True,
                )
        except OrgError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)
        except Exception as e:
            logger.exception("arb member failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    # ---------- settings ----------

    @arbiter.command(name="settings", description="Настройки организации (роли, каналы)")
    @app_commands.describe(
        admin_role="Роль админов",
        referee_role="Роль судей",
        match_category="Категория для комнат матчей",
        match_log_channel="Канал логов матчей",
        evidence_channel="Канал доказательств",
    )
    async def settings(
        self,
        interaction: discord.Interaction,
        admin_role: discord.Role | None = None,
        referee_role: discord.Role | None = None,
        match_category: discord.CategoryChannel | None = None,
        match_log_channel: discord.TextChannel | None = None,
        evidence_channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена. Используйте /arb setup.")
            profile = await org_service.get_or_create_profile(interaction.user)
            member = await org_service.get_member(org.id, profile.id)
            if not member or member.role not in (OrgRole.OWNER, OrgRole.ADMIN):
                raise OrgError("Только Owner/Admin могут менять настройки.")

            fields = {}
            if admin_role is not None:
                fields["admin_role_id"] = str(admin_role.id)
            if referee_role is not None:
                fields["referee_role_id"] = str(referee_role.id)
            if match_category is not None:
                fields["match_category_id"] = str(match_category.id)
            if match_log_channel is not None:
                fields["match_log_channel_id"] = str(match_log_channel.id)
            if evidence_channel is not None:
                fields["evidence_channel_id"] = str(evidence_channel.id)

            s = await org_service.update_settings(org.id, **fields)
            embed = DesignEmbed(category="success", title="Настройки обновлены",
                                description=f"**{org.name}**")
            embed.add_kv("Admin Role", f"<@&{s.admin_role_id}>" if s.admin_role_id else "—")
            embed.add_kv("Referee Role", f"<@&{s.referee_role_id}>" if s.referee_role_id else "—")
            embed.add_kv("Match Category", f"<#{s.match_category_id}>" if s.match_category_id else "—")
            embed.add_kv("Log Channel", f"<#{s.match_log_channel_id}>" if s.match_log_channel_id else "—")
            embed.add_kv("Evidence Channel", f"<#{s.evidence_channel_id}>" if s.evidence_channel_id else "—")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except OrgError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)
        except Exception as e:
            logger.exception("arb settings failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ArbiterCog(bot))
