"""OcrConfirmView — подтверждение OCR-статистики организатором.

Показывает сопоставленных/несопоставленных игроков и по кнопке
«Применить» пишет kills/deaths в temp_match_stats legacy-турнира.
"""

from __future__ import annotations

import logging

import discord
from discord import ui

from config import replace_emojis
from utils.design.components import ButtonRole, design_button
from utils.design.embeds import DesignEmbed
from utils.permissions import is_org_check

logger = logging.getLogger(__name__)


class OcrConfirmView(ui.View):
    def __init__(self, guild_id: int, tournament_id: str, match_type: str,
                 match_index: int, matched: dict[str, dict],
                 unmatched: list[dict], match_label: str,
                 cid_suffix: str = ""):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.tournament_id = tournament_id
        self.match_type = match_type
        self.match_index = match_index
        self.matched = matched
        self.unmatched = unmatched
        self.match_label = match_label
        self.add_item(design_button(
            label="Применить статистику", role=ButtonRole.SUCCESS,
            emoji="check", custom_id=f"ocr_confirm_apply{cid_suffix}",
            callback=self._apply))
        self.add_item(design_button(
            label="Отмена", role=ButtonRole.SECONDARY,
            emoji="cross", custom_id=f"ocr_confirm_cancel{cid_suffix}",
            callback=self._cancel))

    async def _apply(self, interaction: discord.Interaction) -> None:
        if not is_org_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                replace_emojis("❌ Только организатор может применить статистику."),
                ephemeral=True)
            return
        from arbiter.services.ocr_apply import write_stats_to_tournament
        ok = await write_stats_to_tournament(
            self.guild_id, self.tournament_id, self.match_type,
            self.match_index, self.matched)
        if ok:
            await interaction.response.edit_message(
                embed=DesignEmbed(
                    category="success", title="OCR статистика применена",
                    description=(f"Записано игроков: **{len(self.matched)}** "
                                 f"для {self.match_label}.\n"
                                 "Теперь завершите матч кнопкой «Завершить» "
                                 "в arbiter-панели — Elo/история/прогрессия "
                                 "применятся автоматически.")),
                view=None)
        else:
            await interaction.response.edit_message(
                embed=DesignEmbed(category="error", title="Ошибка",
                                  description="Турнир не найден."),
                view=None)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=DesignEmbed(category="neutral", title="OCR отменён",
                              description="Статистика не была записана."),
            view=None)
