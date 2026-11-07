"""LayoutView и шаблоны сообщений дизайн-системы.

DesignView — базовый LayoutView: message с Container-карточкой.
Отправка: channel.send(view=view) — discord.py сам ставит флаг
IS_COMPONENTS_V2. В V2-сообщении нельзя использовать embed/content —
весь текст идёт через TextDisplay внутри Container.
"""

from __future__ import annotations

from typing import Iterable

import discord

from utils.design import colors
from utils.design.containers import chunk_text, section, separator
from utils.design.formatters import muted


class DesignView(discord.ui.LayoutView):
    """LayoutView с одной Container-карточкой.

    Использование:
        view = DesignView(title="Баланс", accent=colors.ECONOMY)
        view.text("Монеты: **1 000**")
        view.sep()
        view.row(btn("Пополнить", style="success", custom_id="topup"))
        await channel.send(view=view)

    Или из готового Container:
        view = DesignView.from_card(card(...))
    """

    def __init__(
        self,
        *,
        timeout: float | None = 180.0,
        accent: int | None = None,
        title: str | None = None,
        spoiler: bool = False,
        container: discord.ui.Container | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        if container is not None:
            self.container = container
        else:
            self.container = discord.ui.Container(
                accent_color=accent if accent is not None else colors.NEUTRAL,
                spoiler=spoiler,
            )
            if title:
                self.container.add_item(
                    discord.ui.TextDisplay(content=f"# {title[:80]}")
                )
                self.container.add_item(separator())
        self.add_item(self.container)

    # --- Наполнение карточки ---
    def text(self, content: str) -> "DesignView":
        for chunk in chunk_text(content):
            self.container.add_item(discord.ui.TextDisplay(content=chunk))
        return self

    def lines(self, *lines: str) -> "DesignView":
        return self.text("\n".join(lines))

    def sep(self, *, large: bool = False) -> "DesignView":
        self.container.add_item(separator(large=large))
        return self

    def row(self, *items: discord.ui.Item) -> "DesignView":
        r = discord.ui.ActionRow()
        for item in items:
            r.add_item(item)
        self.container.add_item(r)
        return self

    def section_text(
        self,
        text: str,
        *,
        thumbnail: str | None = None,
        accessory_button: discord.ui.Button | None = None,
    ) -> "DesignView":
        self.container.add_item(
            section(
                text,
                thumbnail=thumbnail,
                accessory_button=accessory_button,
            )
        )
        return self

    def component(self, item: discord.ui.Item) -> "DesignView":
        self.container.add_item(item)
        return self

    @classmethod
    def from_card(
        cls,
        container: discord.ui.Container,
        *,
        timeout: float | None = 180.0,
    ) -> "DesignView":
        """LayoutView из готового Container."""
        return cls(timeout=timeout, container=container)

    @classmethod
    def simple(
        cls,
        *,
        title: str | None = None,
        body: str | list[str] | None = None,
        accent: int | None = colors.NEUTRAL,
        footer: str | None = None,
        rows: list[discord.ui.ActionRow] | None = None,
        timeout: float | None = 180.0,
    ) -> "DesignView":
        """Типовая карточка: заголовок + текст + опциональные кнопки."""
        view = cls(title=title, accent=accent, timeout=timeout)
        if body is not None:
            view.text("\n".join(body) if isinstance(body, list) else body)
        if footer:
            view.sep()
            view.text(muted(footer))
        if rows:
            for r in rows:
                view.container.add_item(r)
        return view


# ---------------------------------------------------------------------------
# Шаблоны типовых сообщений
# ---------------------------------------------------------------------------

def success_view(
    title: str,
    message: str,
    *,
    details: str | None = None,
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Success Container."""
    body = f"{details}\n\n{message}" if details else message
    return DesignView.simple(
        title=title, body=body, accent=colors.SUCCESS, rows=rows, timeout=timeout
    )


def error_view(
    title: str,
    message: str,
    *,
    details: str | None = None,
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Error Container."""
    body = f"{details}\n\n{message}" if details else message
    return DesignView.simple(
        title=title, body=body, accent=colors.ERROR, rows=rows, timeout=timeout
    )


def warning_view(
    title: str,
    message: str,
    *,
    details: str | None = None,
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Warning Container."""
    body = f"{details}\n\n{message}" if details else message
    return DesignView.simple(
        title=title, body=body, accent=colors.WARNING, rows=rows, timeout=timeout
    )


def info_view(
    title: str,
    message: str,
    *,
    details: str | None = None,
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Information Container."""
    body = f"{details}\n\n{message}" if details else message
    return DesignView.simple(
        title=title, body=body, accent=colors.INFO, rows=rows, timeout=timeout
    )


def profile_view(
    *,
    title: str,
    lines: Iterable[str],
    thumbnail: str | None = None,
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Profile Container — заголовок + avatar-секция + строки."""
    container = CardBuilder(title=title, accent=colors.PROFILE)
    container.text("\n".join(lines))
    if thumbnail:
        container.component(
            discord.ui.MediaGallery(discord.MediaGalleryItem(media=thumbnail))
        )
    if rows:
        container.rows(*rows)
    return DesignView.from_card(container.build(), timeout=timeout)


def economy_view(
    *,
    title: str,
    lines: Iterable[str],
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Economy Container."""
    return DesignView.simple(
        title=title,
        body="\n".join(lines),
        accent=colors.ECONOMY,
        rows=rows,
        timeout=timeout,
    )


def ranking_view(
    *,
    title: str,
    lines: Iterable[str],
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Ranking Container."""
    return DesignView.simple(
        title=title,
        body="\n".join(lines),
        accent=colors.RATING,
        rows=rows,
        timeout=timeout,
    )


def game_view(
    *,
    title: str,
    body: str | list[str],
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Game Container."""
    return DesignView.simple(
        title=title, body=body, accent=colors.GAME, rows=rows, timeout=timeout
    )


def moderation_view(
    *,
    title: str,
    body: str | list[str],
    rows: list[discord.ui.ActionRow] | None = None,
    timeout: float | None = 180.0,
) -> DesignView:
    """Moderation Container."""
    return DesignView.simple(
        title=title, body=body, accent=colors.MODERATION, rows=rows, timeout=timeout
    )


def menu_view(
    *,
    title: str,
    description: str,
    rows: list[discord.ui.ActionRow],
    timeout: float | None = 180.0,
) -> DesignView:
    """Menu Container — заголовок + описание + кнопки/селекты."""
    return DesignView.simple(
        title=title, body=description, accent=colors.INFO, rows=rows, timeout=timeout
    )


# ---------------------------------------------------------------------------
# Хелперы отправки/редактирования
# ---------------------------------------------------------------------------

async def send_card(
    destination,
    view: discord.ui.LayoutView,
    *,
    ephemeral: bool = False,
    **kwargs,
):
    """Отправить V2-сообщение. destination: channel/interaction/webhook.

    Для interaction: interaction.response.send_message(view=view, ephemeral=...)
    Для канала: channel.send(view=view)
    """
    if hasattr(destination, "response") and destination.response is not None:
        return await destination.response.send_message(
            view=view, ephemeral=ephemeral, **kwargs
        )
    return await destination.send(view=view, **kwargs)


async def edit_card(
    interaction: discord.Interaction,
    view: discord.ui.LayoutView,
    *,
    ephemeral: bool = False,
    **kwargs,
):
    """Отредактировать сообщение interaction'а на V2-карточку."""
    if interaction.response.is_done():
        return await interaction.followup.edit_message(
            view=view, **kwargs
        )
    return await interaction.response.edit_message(view=view, **kwargs)
