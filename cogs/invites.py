"""Invite tracker — атрибуция входов по инвайтам + репутация.

Адаптация логики greg6775/Discord-Invite-Tracker под нашего бота:
кэшируем uses() инвайтов на ready, при on_member_join находим инвайт
с выросшим счётчиком, инвайтеру +1 репутация и +1 invite_count.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis
from models.player_stats import PlayerStats
from storage.player_stats_store import player_stats_store
from utils.design import DesignEmbed, emoji

logger = logging.getLogger(__name__)

invites_group = app_commands.Group(
    name="invites", description="Приглашения и репутация"
)


class InviteTracker(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # guild_id -> {invite_code: uses}
        self._invite_cache: dict[int, dict[str, int]] = {}

    # ---------------- invite cache ----------------

    async def _cache_guild_invites(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
            self._invite_cache[guild.id] = {i.code: i.uses for i in invites}
        except discord.Forbidden:
            logger.warning("Нет прав на чтение инвайтов в %s", guild.name)
        except Exception as e:
            logger.warning("Invite cache failed for %s: %s", guild.name, e)

    async def _find_used_invite(self, guild: discord.Guild,
                                member: discord.Member) -> discord.Invite | None:
        """Найти инвайт, по которому зашёл member (diff uses)."""
        try:
            new_invites = await guild.invites()
        except discord.Forbidden:
            return None
        old = self._invite_cache.get(guild.id, {})
        used = None
        for inv in new_invites:
            if old.get(inv.code, 0) < inv.uses:
                used = inv
                break
        # обновляем кэш
        self._invite_cache[guild.id] = {i.code: i.uses for i in new_invites}
        return used

    # ---------------- listeners ----------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._cache_guild_invites(guild)
        logger.info("Invite cache built for %d guilds", len(self.bot.guilds))

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._cache_guild_invites(guild)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        invite = await self._find_used_invite(guild, member)
        inviter = getattr(invite, "inviter", None) if invite else None
        if inviter is None or inviter.bot:
            return

        # +1 репутация и +1 invite_count инвайтеру
        try:
            stats = await player_stats_store.get(guild.id, inviter.id)
            if stats is None:
                stats = PlayerStats(guild_id=guild.id, user_id=inviter.id,
                                    name=inviter.display_name)
            stats.reputation += 1
            stats.invite_count += 1
            await player_stats_store.set(stats)
        except Exception as e:
            logger.error("Invite reward failed: %s", e, exc_info=True)
            return

        logger.info("Invite: %s invited %s in %s (+1 rep)",
                    inviter, member, guild.name)

        # Уведомление в лог-канал (если настроен)
        try:
            import os
            log_channel_id = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
            if log_channel_id:
                channel = guild.get_channel(log_channel_id)
                if channel:
                    embed = DesignEmbed(
                        category="success",
                        title=f"{emoji('team')} Новый участник",
                        description=(
                            f"{member.mention} зашёл по инвайту "
                            f"`{invite.code}` от {inviter.mention}\n"
                            f"Репутация: **+1** (всего {stats.reputation})")),
                    await channel.send(embed=embed)
        except Exception as e:
            logger.warning("Invite log failed: %s", e)

    # ---------------- commands ----------------

    @invites_group.command(name="my", description="Мои приглашения и репутация")
    async def invites_my(self, interaction: discord.Interaction) -> None:
        stats = await player_stats_store.get(interaction.guild_id,
                                             interaction.user.id)
        rep = stats.reputation if stats else 0
        count = stats.invite_count if stats else 0
        embed = DesignEmbed(
            category="info",
            title=f"{emoji('team')} Приглашения",
            description=(
                f"{interaction.user.mention}\n"
                f"• Приглашено игроков: **{count}**\n"
                f"• Репутация: **{rep}**"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @invites_group.command(name="top", description="Топ инвайтеров сервера")
    async def invites_top(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            all_stats = await player_stats_store.get_all(interaction.guild_id)
        except Exception:
            all_stats = []
        top = sorted(
            (s for s in all_stats if getattr(s, "invite_count", 0) > 0),
            key=lambda s: s.invite_count, reverse=True)[:10]
        if not top:
            embed = DesignEmbed(category="neutral", title="Инвайты",
                                description="Пока никто не приглашал игроков.")
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, s in enumerate(top):
                medal = medals[i] if i < 3 else f"`{i + 1}.`"
                lines.append(f"{medal} <@{s.user_id}> — **{s.invite_count}** инвайтов • репутация {s.reputation}")
            embed = DesignEmbed(
                category="info", title=f"{emoji('crown')} Топ инвайтеров",
                description="\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(InviteTracker(bot))
    bot.tree.add_command(invites_group)
