"""Cog Reputation System 2.0: команды аналитики + отслеживание
приглашений и бустов.

- /reputation profile|history|stats — аналитика REP (Components V2).
- /invites — статистика приглашений пользователя.
- /rep — реализован в cogs/tournament.py через services.reputation.
- on_member_join — атрибуция приглашения и кандидат на +5 REP.
- on_member_update — обнаружение старта/остановки буста.
- Фоновый sweep подтверждает награды (72ч / 7 дней).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from config import REP_INVITE_AMOUNT
from cogs.groups import bind_commands, reputation_group
from services import reputation as rep_svc
from storage.player_stats_store import player_stats_store
from storage.reputation_store import (
    SRC_BOOST,
    SRC_INVITE,
    SRC_MANUAL,
    reputation_store,
)
from utils.design import colors
from utils.design.layouts import DesignView

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL = 60  # секунд

_SOURCE_TITLES = {
    SRC_MANUAL: "через /rep",
    SRC_INVITE: "приглашение",
    SRC_BOOST: "буст сервера",
}


def _card(title: str, lines: list[str], accent: int = colors.NEUTRAL) -> DesignView:
    return DesignView.simple(
        title=title, body="\n".join(lines), accent=accent, timeout=None
    )


class ReputationCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot
        self._sweep_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        bind_commands(
            reputation_group, self, ["profile", "history", "stats"]
        )
        # Кэш инвайтов: guild_id -> {code: Invite}
        if not hasattr(self.bot, "_rep_invite_cache"):
            self.bot._rep_invite_cache = {}
        for guild in self.bot.guilds:
            await self._refresh_invite_cache(guild)
        self._sweep_task = asyncio.create_task(self._reward_sweeper())

    def cog_unload(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()

    async def _refresh_invite_cache(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
            self.bot._rep_invite_cache[guild.id] = {i.code: i for i in invites}
        except Exception as exc:
            logger.warning("Invite cache refresh failed for guild %s: %s", guild.id, exc)

    async def _reward_sweeper(self) -> None:
        """Периодически подтверждать награды за приглашения и бусты."""
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL)
                # Поддерживаем кэш инвайтов свежим (invite create/delete)
                for guild in self.bot.guilds:
                    await self._refresh_invite_cache(guild)
                await rep_svc.process_due_invites(self.bot)
                await rep_svc.process_due_boosts(self.bot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Reputation sweeper error: %s", exc)

    # ------------------------------------------------------------------
    # События Discord
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cache = getattr(self.bot, "_rep_invite_cache", {})
        before = cache.get(member.guild.id, {})
        try:
            await rep_svc.record_member_join(self.bot, member, before)
        except Exception as exc:
            logger.error("Invite tracking failed for %s: %s", member.id, exc)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        try:
            await self._handle_boost_change(before, after)
        except Exception as exc:
            logger.error("Boost tracking failed for %s: %s", after.id, exc)

    async def _handle_boost_change(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """premium_since: None -> ts = старт буста; ts -> None/новый = стоп."""
        b = getattr(before, "premium_since", None)
        a = getattr(after, "premium_since", None)
        if a is not None and b is None:
            # Новый буст — создаём ожидающий период проверки
            await rep_svc.record_boost_start(after, a)
            logger.info("Boost started for %s in guild %s", after.id, after.guild.id)
        elif a is None and b is not None:
            # Буст остановлен — сбрасываем pending-период
            await rep_svc.record_boost_end(after.guild.id, after.id)
        elif a is not None and b is not None and a != b:
            # premium_since изменился (перезапуск буста) — новый период
            await rep_svc.record_boost_start(after, a)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._refresh_invite_cache(guild)

    # ------------------------------------------------------------------
    # Команды аналитики
    # ------------------------------------------------------------------

    @reputation_group.command(name="profile", description="Репутация игрока")
    @app_commands.describe(user="Участник сервера")
    async def rep_profile(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        guild_id = interaction.guild_id
        stats = await player_stats_store.get(guild_id, user.id)
        total = int(getattr(stats, "reputation", 0) or 0) if stats else 0

        breakdown = await reputation_store.get_source_breakdown(guild_id, user.id)
        invite_stats = await reputation_store.get_user_invite_stats(guild_id, user.id)
        given = await reputation_store.count_manual_given(guild_id, user.id)

        tracked = sum(breakdown.values())
        legacy = max(0, total - tracked)

        lines = [
            f"Игрок: {user.mention}",
            f"Всего репутации: {total}",
        ]
        if breakdown.get(SRC_INVITE):
            lines.append(f"Получено за приглашения: {breakdown[SRC_INVITE]}")
        if breakdown.get(SRC_MANUAL):
            lines.append(f"Получено через /rep: {breakdown[SRC_MANUAL]}")
        if breakdown.get(SRC_BOOST):
            lines.append(f"Получено за буст: {breakdown[SRC_BOOST]}")
        for src, val in breakdown.items():
            if src not in (SRC_INVITE, SRC_MANUAL, SRC_BOOST) and val:
                lines.append(f"Получено ({src}): {val}")
        if legacy:
            lines.append(f"Репутация до обновления: {legacy}")
        lines.append(f"Подтверждённых приглашений: {invite_stats['rewarded']}")
        lines.append(f"Выдано репутации другим: {given}")

        await interaction.response.send_message(
            view=_card("РЕПУТАЦИЯ УЧАСТНИКА", lines, colors.RATING),
            ephemeral=False,
        )

    @reputation_group.command(name="history", description="История начислений репутации")
    @app_commands.describe(user="Участник сервера")
    async def rep_history(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        guild_id = interaction.guild_id
        txs = await reputation_store.get_transactions(guild_id, user.id, limit=10)
        if not txs:
            await interaction.response.send_message(
                view=_card(
                    "ИСТОРИЯ РЕПУТАЦИИ",
                    [f"{user.mention}: операций пока нет."],
                ),
                ephemeral=True,
            )
            return

        lines = []
        for tx in txs:
            src = _SOURCE_TITLES.get(tx["source"], tx["source"].lower())
            actor = tx.get("actor_user_id") or 0
            when = tx.get("created_at")
            if isinstance(when, datetime):
                ts = int(when.timestamp())
            else:
                ts = 0
            date_str = f"<t:{ts}:d>" if ts else "—"
            extra = ""
            if tx["source"] == SRC_MANUAL and actor and actor != user.id:
                extra = f" — от <@{actor}>"
            lines.append(
                f"+{tx['amount']} REP — {src}{extra} · {date_str}"
            )
        await interaction.response.send_message(
            view=_card("ИСТОРИЯ РЕПУТАЦИИ", [f"{user.mention}:"] + lines),
            ephemeral=False,
        )

    @reputation_group.command(name="stats", description="Общая статистика репутации сервера")
    async def rep_stats(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        data = await reputation_store.get_guild_rep_stats(guild_id)

        lines = [f"Участников с репутацией: {data['users_with_rep']}"]
        by_src = data["by_source"]
        if by_src:
            for src in (SRC_MANUAL, SRC_INVITE, SRC_BOOST):
                item = by_src.get(src)
                if item:
                    title = _SOURCE_TITLES.get(src, src)
                    lines.append(
                        f"{title}: {item['total']} REP ({item['ops']} опр.)"
                    )
            for src, item in by_src.items():
                if src not in (SRC_MANUAL, SRC_INVITE, SRC_BOOST):
                    lines.append(f"{src}: {item['total']} REP ({item['ops']} опр.)")
        else:
            lines.append("Операций в журнале пока нет.")
        if data["legacy_rep"]:
            lines.append(f"Репутация до обновления (без истории): {data['legacy_rep']}")

        await interaction.response.send_message(
            view=_card("СТАТИСТИКА РЕПУТАЦИИ", lines, colors.INFO),
            ephemeral=False,
        )

    @app_commands.command(name="invites", description="Статистика приглашений пользователя")
    @app_commands.describe(user="Участник сервера")
    async def invites(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        guild_id = interaction.guild_id
        stats = await reputation_store.get_user_invite_stats(guild_id, user.id)

        lines = [
            f"Приглашено пользователей: {stats['invited']}",
            f"Подтверждённых приглашений: {stats['rewarded']}",
            f"Ожидают проверки: {stats['pending']}",
            f"Отклонено: {stats['rejected']}",
            f"REP за приглашения: {stats['rewarded'] * REP_INVITE_AMOUNT}",
        ]
        await interaction.response.send_message(
            view=_card("ПРИГЛАШЕНИЯ", [f"{user.mention}:"] + lines, colors.INFO),
            ephemeral=False,
        )


async def setup(bot: "TournamentBot") -> None:
    await bot.add_cog(ReputationCog(bot))
