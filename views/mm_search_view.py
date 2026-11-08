"""Персистентная панель Ranked Queue (Components V2, без эмодзи).

Сообщение-панель живёт в канале, где запущен /mm squad. Кнопки имеют
constant custom_id и работают через persistent views: после рестарта
бот регистрирует QueueControlsView на сохранённый message_id.

Баннер queue_banner.png крепится к сообщению один раз при создании
и отображается через MediaGallery (attachment://); последующие
обновления — только edit view, вложение сохраняется.
"""

from __future__ import annotations

import logging

import discord

from models.mm_search import MMSearch
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

JOIN_CUSTOM_ID = "mmq_join:{gid}:{cid}"
LEAVE_CUSTOM_ID = "mmq_leave:{gid}:{cid}"
BANNER_FILENAME = "queue_banner.png"


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
    """Кнопка «Покинуть очередь»."""

    def __init__(self, guild_id: int, channel_id: int):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Покинуть очередь",
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


def _roster_lines(search: MMSearch) -> str:
    """Все слоты очереди: 01..NN с игроками или 'Свободно'."""
    required = search.required_players
    lines: list[str] = []
    for i in range(required):
        num = f"{i + 1:02d}"
        if i < len(search.players):
            name = search.players[i]
            uid = search.player_user_ids.get(name, 0)
            entry = f"<@{uid}>" if uid else name
        else:
            entry = "Свободно"
        lines.append(f"{num}. {entry}")
    return "\n".join(lines)


def build_queue_panel(
    search: MMSearch, ready_count: int | None = None
) -> discord.ui.LayoutView:
    """Построить V2-панель очереди (open/locked)."""
    view = DesignView(timeout=None)

    # Баннер — MediaGallery поверх карточки (файл крепится к сообщению)
    view.component(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(media=f"attachment://{BANNER_FILENAME}")
        )
    )
    view.text("## RANKED MATCHMAKING")
    view.text("-# FREE FIRE · CS 4×4 · SOLO QUEUE")
    view.sep()

    if search.is_locked:
        view.text("## СТАТУС ОЧЕРЕДИ")
        view.lines(
            "Статус: Проверка готовности",
            f"Матч #{search.match_num}" if search.match_num else "Матч —",
            (
                f"Подтвердили участие: {ready_count}/{search.required_players}"
                if ready_count is not None
                else "Подтвердили участие: —"
            ),
            "Набор временно закрыт.",
        )
        view.sep()
        view.text(
            "-# Очередь откроется после запуска или отмены матча."
        )
    else:
        required = search.required_players
        current = len(search.players)
        view.text("## ПОИСК ИГРОКОВ")
        view.lines(
            f"Участников: {current}/{required}",
            f"Свободных мест: {max(0, required - current)}",
        )
        view.sep()
        view.text(f"## УЧАСТНИКИ ОЧЕРЕДИ\n{_roster_lines(search)}")
        view.sep()
        view.text("## АВТОМАТИЧЕСКИЙ ПОДБОР")
        view.lines(
            "Матч начнётся после набора 8 участников и подтверждения готовности.",
            "Распределение команд выполняется по Skill Rating.",
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
