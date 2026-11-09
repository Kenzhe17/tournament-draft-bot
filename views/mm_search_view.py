"""Персистентная панель Ranked Queue (Components V2, без эмодзи).

Сообщение-панель живёт в канале, где запущен /mm squad. В одной
карточке показываются ДВЕ очереди: «Обычка» (normal) и «БЕЗ РУИН»
(no_ruin, SR 5.5–10). Кнопки имеют constant custom_id и работают
через persistent views: после рестарта бот регистрирует
QueueControlsView на сохранённый message_id.

Кастомные ID обычной очереди сохранены со старых панелей
(``mmq_join:{gid}:{cid}`` / ``mmq_leave:{gid}:{cid}``) — обратная
совместимость. У «БЕЗ РУИН» ID с суффиксом ``_nr``.

Баннер queue_banner.png крепится к сообщению один раз при создании
и отображается через MediaGallery (attachment://); последующие
обновления — только edit view, вложение сохраняется.
"""

from __future__ import annotations

import logging

import discord

from models.mm_search import (
    MMSearch,
    QUEUE_TYPE_NO_RUIN,
    QUEUE_TYPE_NORMAL,
    NO_RUIN_MAX_SR,
    NO_RUIN_MIN_SR,
)
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

JOIN_CUSTOM_ID = "mmq_join:{gid}:{cid}"
LEAVE_CUSTOM_ID = "mmq_leave:{gid}:{cid}"
JOIN_NR_CUSTOM_ID = "mmq_join_nr:{gid}:{cid}"
LEAVE_NR_CUSTOM_ID = "mmq_leave_nr:{gid}:{cid}"
BANNER_FILENAME = "queue_banner.png"

_QUEUE_TITLES = {
    QUEUE_TYPE_NORMAL: "ОБЫЧКА",
    QUEUE_TYPE_NO_RUIN: f"БЕЗ РУИН · SR {NO_RUIN_MIN_SR}–{NO_RUIN_MAX_SR:g}",
}


def _cog(interaction: discord.Interaction):
    return getattr(interaction.client, "mm_cog", None)


class QueueJoinButton(discord.ui.Button):
    """Кнопка входа в очередь («Войти Обычка» / «Войти БЕЗ РУИН»)."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        queue_type: str = QUEUE_TYPE_NORMAL,
        disabled: bool = False,
    ):
        self.queue_type = queue_type
        if queue_type == QUEUE_TYPE_NO_RUIN:
            label = "Войти БЕЗ РУИН | SR 5.5+"
            cid = JOIN_NR_CUSTOM_ID.format(gid=guild_id, cid=channel_id)
        else:
            label = "Войти Обычка"
            cid = JOIN_CUSTOM_ID.format(gid=guild_id, cid=channel_id)
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            custom_id=cid,
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
        await cog.handle_queue_join(
            interaction, self.guild_id, self.channel_id, self.queue_type
        )


class QueueLeaveButton(discord.ui.Button):
    """Кнопка выхода из очереди."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        queue_type: str = QUEUE_TYPE_NORMAL,
    ):
        self.queue_type = queue_type
        if queue_type == QUEUE_TYPE_NO_RUIN:
            label = "Выйти БЕЗ РУИН"
            cid = LEAVE_NR_CUSTOM_ID.format(gid=guild_id, cid=channel_id)
        else:
            label = "Выйти Обычка"
            cid = LEAVE_CUSTOM_ID.format(gid=guild_id, cid=channel_id)
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            custom_id=cid,
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
        await cog.handle_queue_leave(
            interaction, self.guild_id, self.channel_id, self.queue_type
        )


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


def _queue_status_lines(search: MMSearch | None, ready_count: int | None) -> list[str]:
    """Строки статуса одной очереди."""
    title = _QUEUE_TITLES.get(
        search.queue_type if search else QUEUE_TYPE_NORMAL, "ОЧЕРЕДЬ"
    )
    if search is None:
        return [f"## {title}", "Статус: Ожидание игроков", "Участников: 0/8"]
    if search.is_waiting_channel:
        return [
            f"## {title}",
            "Статус: Ожидание канала",
            f"Матч #{search.match_num}" if search.match_num else "Матч —",
            "Все каналы матчей сейчас заняты.",
            "Матч начнётся автоматически, как только освободится слот.",
        ]
    if search.is_locked:
        return [
            f"## {title}",
            "Статус: Проверка готовности",
            f"Матч #{search.match_num}" if search.match_num else "Матч —",
            (
                f"Подтвердили участие: {ready_count}/{search.required_players}"
                if ready_count is not None
                else "Подтвердили участие: —"
            ),
            "Набор временно закрыт.",
        ]
    required = search.required_players
    current = len(search.players)
    return [
        f"## {title}",
        f"Участников: {current}/{required}",
        f"Свободных мест: {max(0, required - current)}",
    ]


def build_channel_panel(
    guild_id: int,
    channel_id: int,
    searches: dict[str, MMSearch],
    ready_counts: dict[str, int | None] | None = None,
    with_banner: bool = True,
) -> discord.ui.LayoutView:
    """V2-панель с обеими очередями канала (Обычка + БЕЗ РУИН)."""
    view = DesignView(timeout=None)

    if with_banner:
        # Баннер — MediaGallery поверх карточки (файл крепится к сообщению)
        view.component(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=f"attachment://{BANNER_FILENAME}")
            )
        )
        view.sep()

    ready_counts = ready_counts or {}
    normal = searches.get(QUEUE_TYPE_NORMAL)
    no_ruin = searches.get(QUEUE_TYPE_NO_RUIN)

    for qtype in (QUEUE_TYPE_NORMAL, QUEUE_TYPE_NO_RUIN):
        search = searches.get(qtype)
        view.lines(*_queue_status_lines(search, ready_counts.get(qtype)))
        view.sep()
        if search is not None and not search.is_locked:
            view.text(f"### Участники\n{_roster_lines(search)}")
        elif search is not None:
            view.text("### Участники\n_(набор закрыт)_")
        else:
            view.text("### Участники\n_(пока пусто)_")
        view.sep()

    # Кнопки: вход/выход для каждой очереди
    for qtype in (QUEUE_TYPE_NORMAL, QUEUE_TYPE_NO_RUIN):
        search = searches.get(qtype)
        locked = bool(search and search.is_locked)
        view.row(
            QueueJoinButton(guild_id, channel_id, qtype, disabled=locked),
            QueueLeaveButton(guild_id, channel_id, qtype),
        )
    view.text("-# powered by Nota")
    return view


def build_queue_panel(
    search: MMSearch, ready_count: int | None = None
) -> discord.ui.LayoutView:
    """Построить V2-панель очереди (open/locked).

    Совместимость: одиночный поиск — показываем только его очередь.
    """
    return build_channel_panel(
        search.guild_id,
        search.channel_id,
        {search.queue_type or QUEUE_TYPE_NORMAL: search},
        {search.queue_type or QUEUE_TYPE_NORMAL: ready_count},
    )


def build_queue_controls(guild_id: int, channel_id: int) -> discord.ui.View:
    """Persistent view с кнопками панели — для bot.add_view после рестарта.

    Содержит только интерактивные элементы (custom_id совпадают с
    кнопками внутри LayoutView-панели); Discord сопоставляет их по
    custom_id при нажатии. Регистрировать один раз на message_id.
    """
    view = discord.ui.View(timeout=None)
    for qtype in (QUEUE_TYPE_NORMAL, QUEUE_TYPE_NO_RUIN):
        view.add_item(QueueJoinButton(guild_id, channel_id, qtype))
        view.add_item(QueueLeaveButton(guild_id, channel_id, qtype))
    return view
