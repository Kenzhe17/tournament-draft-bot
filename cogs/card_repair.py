"""Owner-only восстановление публичных карточек турниров.

``/cardrepair scan`` — read-only диагностика: для каждого
активного турнира проверить, что его message_id указывает
на карточку турнира, а не на служебный текст (инцидент
«Изменение отменено.»).

``/cardrepair repair`` — сначала scan, затем force-обновление
подтверждённо повреждённых сообщений штатным renderer'ом.
Данные турниров не изменяются; MESSAGE_MISSING не трогаем.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from services import card_repair
from utils.permissions import is_bot_owner

logger = logging.getLogger(__name__)


class CardRepairCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    cardrepair = app_commands.Group(
        name="cardrepair",
        description="Восстановление карточек турниров (только владелец)",
    )

    @cardrepair.command(name="scan", description="Диагностика карточек (read-only)")
    @is_bot_owner()
    async def scan_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            report = await card_repair.scan(self.bot)
        except Exception as exc:
            logger.exception("cardrepair scan failed")
            await interaction.followup.send(
                f"Scan failed: {exc}", ephemeral=True)
            return
        bad = [e for e in report
               if e["status"] not in (card_repair.HEALTHY,)]
        lines = [
            f"**Scan: {len(report)} карточек**",
            "HEALTHY: "
            f"{sum(1 for e in report if e['status'] == card_repair.HEALTHY)}"
            f" · Повреждено: "
            f"{sum(1 for e in report if e['status'] == card_repair.DAMAGED)}"
            f" · MESSAGE_MISSING: "
            f"{sum(1 for e in report if e['status'] == card_repair.MESSAGE_MISSING)}"
            f" · FORBIDDEN: "
            f"{sum(1 for e in report if e['status'] == card_repair.FORBIDDEN)}"
            f" · UNKNOWN/ERROR: "
            f"{sum(1 for e in report if e['status'] in (card_repair.UNKNOWN, card_repair.ERROR))}",
        ]
        for e in bad:
            lines.append(
                f"- `{e['tournament_id']}` ({e['kind']}/{e['phase']}): "
                f"{e['detail']}")
        await interaction.followup.send(
            "\n".join(lines)[:1900], ephemeral=True)

    @cardrepair.command(name="repair", description="Восстановить повреждённые карточки")
    @is_bot_owner()
    async def repair_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await card_repair.repair(self.bot)
        except Exception as exc:
            logger.exception("cardrepair repair failed")
            await interaction.followup.send(
                f"Repair failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            card_repair.format_report(result), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CardRepairCog(bot))
