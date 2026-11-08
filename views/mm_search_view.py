"""Персистентная панель Ranked Queue (Components V2, без эмодзи).

Сообщение-панель живёт в канале, где запущен /mm squad. Кнопки имеют
constant custom_id и работают через persistent views: после рестарта
бот регистрирует QueueControlsView на сохранённый message_id.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from models.mm_search import MMSearch, QUEUE_LOCKED_READY_CHECK
from utils.design.layouts import DesignView

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

JOIN_CUSTOM_ID = "mmq_join:{gid}:{cid}"
LEAVE_CUSTOM_ID = "mmq_leave:{gid}:{cid}"


def _cog(interaction: discord.Interaction):
    return getattr(interaction.client, "mm_cog", None)


class QueueJoinButton(discord.ui.Button):
    """Кнопка «Войти в очередь»."""

    def __init__(self, guild_id: int, channel_id: int, disabled: bool = False):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Войти в очередь",
            custom_id=JOIN_CUSTOM_ID.format(gid=guild_id, cid=channel_id),
            disabled=disabled,
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message(
                "Система временно недоступна.", ephemeral=True
            )
            return
        await cog.handle_queue_join(interaction, self.guild_id, self.channel_id)


class QueueLeaveButton(discord.ui.Button):
    """Кнопка «Выйти из очереди»."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Выйти из очереди",
            custom_id=LEAVE_CUSTOM_ID.format(gid=guild_id, cid=channel_id),
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _cog(interaction)
        if cog is None:
            await interaction.response.send_message(
                "Система временно недоступна.", ephemeral=True
            )
            return
        await cog.handle_queue_leave(interaction, self.guild_id, self.channel_id)


def build_queue_panel(search: MMSearch) -> discord.ui.LayoutView:
    """Построить V2-панель очереди (open/locked)."""
    view = DesignView(title="RANKED MATCHMAKING", timeout=None)
    view.text("FREE FIRE · CS 4×4")
    view.text("Соревновательный подбор команд по Skill Rating")
    view.sep()

    if search.is_locked:
        view.text(
            "## СТАТУС ОЧЕРЕДИ\n"
            "Проверка готовности\n"
            f"Матч #{search.match_num or '—'}\n"
            "Набор временно закрыт"
        )
        view.sep()
        view.text(
            "-# Восемь участников подтверждают готовность к матчу.\n"
            "-# Очередь откроется после запуска или отмены матча."
        )
    else:
        required = search.required_players
        current = len(search.players)
        lines = [
            "## СТАТУС ОЧЕРЕДИ",
            "Поиск игроков",
            f"Участников: {current}/{required}",
            f"Необходимо ещё: {max(0, required - current)}",
        ]
        view.text("\n".join(lines))
        if search.players:
            view.sep()
            roster = "\n".join(
                f"{i}. {name}" for i, name in enumerate(search.players, 1)
            )
            view.text(f"## УЧАСТНИКИ\n{roster}")
        view.sep()
        view.text(
            "-# После набора восьми участников матч будет сформирован автоматически."
        )

    view.row(
        QueueJoinButton(search.guild_id, search.channel_id,
                        disabled=search.is_locked),
        QueueLeaveButton(search.guild_id, search.channel_id),
    )
    return view


def build_queue_controls(guild_id: int, channel_id: int) -> discord.ui.View:
    """Persistent view с кнопками панели — для bot.add_view после рестарта.

    Содержит только интерактивные элементы (custom_id совпадают с
    кнопками внутри LayoutView-панели); Discord сопоставляет их по
    custom_id при нажатии.
    """
    view = discord.ui.View(timeout=None)
    view.add_item(QueueJoinButton(guild_id, channel_id))
    view.add_item(QueueLeaveButton(guild_id, channel_id))
    return view
