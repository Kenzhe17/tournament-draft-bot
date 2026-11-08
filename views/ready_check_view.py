"""View и embed для фазы Ready Check («НАЙДЕН МАТЧ»)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from utils.design import compose

from models.ready_check import ReadyCheck, ReadyCheckStatus

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

PNG_FILENAME = "match_found.png"


def _mention(player) -> str:
    return f"<@{player.user_id}>"


def _roster_line(players) -> str:
    parts = []
    for p in players:
        mark = " ✅" if p.is_ready else ""
        parts.append(_mention(p) + mark)
    return " ".join(parts)


def build_ready_embed(rc: ReadyCheck) -> discord.Embed:
    """Embed карточки Ready Check (изображение + статусы)."""
    embed = discord.Embed(
        title="🏆 НАЙДЕН МАТЧ",
        color=discord.Color.from_rgb(88, 66, 180),
    )
    embed.set_image(url=f"attachment://{PNG_FILENAME}")

    status = rc.status
    if status == ReadyCheckStatus.READY_CHECK:
        desc = (
            f"**Команда A — фиолетовая**\n{_roster_line(rc.team_a)}\n\n"
            f"**Команда B — синяя**\n{_roster_line(rc.team_b)}\n\n"
            f"**Готовность:** {rc.ready_count}/8\n"
            f"**Осталось времени:** <t:{int(rc.deadline)}:R>"
        )
    elif status == ReadyCheckStatus.ALL_READY:
        desc = (
            f"**Команда A — фиолетовая**\n{_roster_line(rc.team_a)}\n\n"
            f"**Команда B — синяя**\n{_roster_line(rc.team_b)}\n\n"
            "✅ **Все игроки готовы — 8/8.**\nМатч запускается…"
        )
    elif status == ReadyCheckStatus.LAUNCHING:
        desc = (
            f"**Команда A — фиолетовая**\n{_roster_line(rc.team_a)}\n\n"
            f"**Команда B — синяя**\n{_roster_line(rc.team_b)}\n\n"
            "✅ **Все игроки готовы — 8/8.**\nЗапуск матча…"
        )
    elif status == ReadyCheckStatus.DECLINED:
        decliner = next((p for p in rc.players if p.declined), None)
        who = _mention(decliner) if decliner else "Игрок"
        desc = (
            f"**Команда A — фиолетовая**\n{_roster_line(rc.team_a)}\n\n"
            f"**Команда B — синяя**\n{_roster_line(rc.team_b)}\n\n"
            f"❌ **Матч отменён:** {who} отказался от участия.\n"
            "Остальные игроки возвращены в очередь."
        )
    elif status == ReadyCheckStatus.TIMEOUT:
        desc = (
            f"**Команда A — фиолетовая**\n{_roster_line(rc.team_a)}\n\n"
            f"**Команда B — синяя**\n{_roster_line(rc.team_b)}\n\n"
            f"⏰ **Подтверждение не завершено — {rc.ready_count}/8.**\n"
            "Матч отменён. Готовые игроки возвращены в очередь."
        )
    else:  # LAUNCHED / CANCELLED
        desc = (
            f"**Команда A — фиолетовая**\n{_roster_line(rc.team_a)}\n\n"
            f"**Команда B — синяя**\n{_roster_line(rc.team_b)}\n\n"
            "🏁 **Матч запущен.**"
            if status == ReadyCheckStatus.LAUNCHED
            else f"❌ **Матч отменён.** {rc.cancel_reason}".strip()
        )
    embed.description = desc
    return embed


class ReadyCheckView(discord.ui.View):
    """Кнопки «Готов» / «Отказаться» для Ready Check.

    Persistent: custom_id содержит match_id, view регистрируется
    через bot.add_view при создании и при восстановлении после рестарта.
    """

    def __init__(self, bot: TournamentBot, match_id: str, disabled: bool = False):
        super().__init__(timeout=None)
        self.bot = bot
        self.match_id = match_id

        self.ready_button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Готов",
            emoji="✅",
            custom_id=f"rc_ready:{match_id}",
            disabled=disabled,
        )
        self.ready_button.callback = self._on_ready
        self.add_item(self.ready_button)

        self.decline_button = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="Отказаться",
            emoji="❌",
            custom_id=f"rc_decline:{match_id}",
            disabled=disabled,
        )
        self.decline_button.callback = self._on_decline
        self.add_item(self.decline_button)

    async def _on_ready(self, interaction: discord.Interaction) -> None:
        cog = getattr(self.bot, "mm_cog", None)
        if cog is None:
            await interaction.response.send_message("❌ Система временно недоступна.", ephemeral=True)
            return
        await cog.handle_ready_click(interaction, self.match_id)

    async def _on_decline(self, interaction: discord.Interaction) -> None:
        cog = getattr(self.bot, "mm_cog", None)
        if cog is None:
            await interaction.response.send_message("❌ Система временно недоступна.", ephemeral=True)
            return
        await cog.handle_decline_click(interaction, self.match_id)


def build_ready_view(bot: TournamentBot, match_id: str, disabled: bool = False) -> ReadyCheckView:
    return ReadyCheckView(bot, match_id, disabled=disabled)


def ready_card(rc: ReadyCheck, bot: TournamentBot, disabled: bool = False) -> discord.ui.LayoutView:
    """Полная V2-карточка: embed (с картинкой) + кнопки."""
    return compose(build_ready_embed(rc), build_ready_view(bot, rc.match_id, disabled=disabled))
