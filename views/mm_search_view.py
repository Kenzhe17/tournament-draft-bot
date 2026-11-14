"""Персистентная панель Ranked Queue (Components V2, без эмодзи).

Сообщение-панель живёт в канале, где запущен /mm squad. Кнопки имеют
constant custom_id и работают через persistent views: после рестарта
бот регистрирует QueueControlsView на сохранённый message_id.

"""

from __future__ import annotations

import logging

import discord

from models.mm_search import MMSearch
from utils.design.layouts import DesignView

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
    """Все слоты очереди: 01..NN с псевдонимами или 'Свободно'.

    В очереди показываются только анонимные псевдонимы — реальные
    Discord-ники и упоминания скрыты до формирования матча.
    """
    required = search.required_players
    lines: list[str] = []
    for i in range(required):
        num = f"{i + 1:02d}"
        if i < len(search.players):
            name = search.players[i]
            entry = search.alias_for(name)
        else:
            entry = "Свободно"
        lines.append(f"{num}. {entry}")
    return "\n".join(lines)


def build_queue_panel(
    search: MMSearch, ready_count: int | None = None
) -> discord.ui.LayoutView:
    """Построить V2-панель очереди (open/locked)."""
    view = DesignView(timeout=None)

    if search.is_waiting_channel:
        view.text("## СТАТУС ОЧЕРЕДИ")
        view.lines(
            "Статус: Ожидание канала",
            f"Матч #{search.match_num}" if search.match_num else "Матч —",
            "Все каналы матчей сейчас заняты.",
            "Матч начнётся автоматически, как только освободится слот.",
        )
        view.sep()
        view.text(
            "-# Набор закрыт. Игроки остаются в матче — ждать ничего не нужно."
        )
    elif search.is_locked:
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
        lines = [
            f"Участников: {current}/{required}",
            f"Свободных мест: {max(0, required - current)}",
        ]
        # Динамический дедлайн сессии (Discord timestamp — без
        # пер-редактирования каждую секунду)
        deadline = search.deadline_at
        if search.timer_started and deadline:
            limit_min = max(1, int(search.effective_limit_seconds // 60))
            lines.append(f"Максимальное ожидание: {limit_min} минут")
            lines.append(f"Поиск завершится: <t:{int(deadline)}:R>")
        view.lines(*lines)
        view.sep()
        view.text(f"## УЧАСТНИКИ В ОЧЕРЕДИ\n{_roster_lines(search)}")
        # Долгое ожидание — напоминание без пингов
        if search.session_elapsed >= 600:
            view.text(
                "-# Поиск продолжается. Убедитесь, что сможете "
                "подтвердить готовность при нахождении матча."
            )

    view.row(
        QueueJoinButton(search.guild_id, search.channel_id,
                        disabled=search.is_locked),
        QueueLeaveButton(search.guild_id, search.channel_id),
    )
    view.text("-# powered by Nota")
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
