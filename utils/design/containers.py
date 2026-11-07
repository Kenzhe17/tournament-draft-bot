"""Container Builder дизайн-системы r!z3 Minimal Dark UI.

Собирает discord.ui.Container из текста, секций, разделителей
и интерактивных рядов (ActionRow с кнопками/select).
"""

from __future__ import annotations

import discord

from utils.design import colors
from utils.design.constants import MAX_TEXT_LENGTH
from utils.design.formatters import truncate


def text_display(content: str) -> discord.ui.TextDisplay:
    """TextDisplay с автоматической разбивкой длинного текста."""
    return discord.ui.TextDisplay(content=content)


def separator(*, large: bool = False, visible: bool = True) -> discord.ui.Separator:
    """Тонкий разделитель между смысловыми секциями."""
    spacing = (
        discord.SeparatorSpacing.large if large else discord.SeparatorSpacing.small
    )
    return discord.ui.Separator(spacing=spacing, visible=visible)


def section(
    text: str,
    *,
    thumbnail: str | None = None,
    accessory_button: discord.ui.Button | None = None,
) -> discord.ui.Section:
    """Section с текстом и опциональным thumbnail/кнопкой-аксессуаром."""
    display = discord.ui.TextDisplay(content=text)
    accessory = None
    if thumbnail:
        accessory = discord.ui.Thumbnail(media=thumbnail)
    elif accessory_button is not None:
        accessory = accessory_button
    return discord.ui.Section(display, accessory=accessory)


def chunk_text(text: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Разбить текст на куски <= limit по границам строк."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Строка длиннее лимита — режем жёстко
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
    if current:
        chunks.append(current)
    return chunks


class CardBuilder:
    """Пошаговый builder Container'а.

    Пример:
        card = CardBuilder(title="Баланс", accent=colors.ECONOMY)
        card.text("Монеты: **1 000**")
        card.sep()
        card.row(btn("Пополнить", style="success", custom_id="topup"))
        container = card.build()
    """

    def __init__(
        self,
        *,
        title: str | None = None,
        accent: int | None = colors.NEUTRAL,
        spoiler: bool = False,
    ) -> None:
        self._children: list[discord.ui.Item] = []
        self._accent = accent
        self._spoiler = spoiler
        if title:
            self._children.append(
                discord.ui.TextDisplay(content=f"# {truncate(title, 80)}")
            )
            self._children.append(separator())

    # --- Контент ---
    def text(self, content: str) -> "CardBuilder":
        """Добавить текстовый блок (поддерживает \n\n и списки)."""
        for chunk in chunk_text(content):
            self._children.append(discord.ui.TextDisplay(content=chunk))
        return self

    def lines(self, *lines: str) -> "CardBuilder":
        """Добавить строки как один текстовый блок."""
        return self.text("\n".join(lines))

    def section_text(
        self,
        text: str,
        *,
        thumbnail: str | None = None,
        accessory_button: discord.ui.Button | None = None,
    ) -> "CardBuilder":
        self._children.append(
            section(text, thumbnail=thumbnail, accessory_button=accessory_button)
        )
        return self

    def sep(self, *, large: bool = False) -> "CardBuilder":
        self._children.append(separator(large=large))
        return self

    def row(self, *items: discord.ui.Item) -> "CardBuilder":
        """Добавить ActionRow с компонентами (кнопки/select)."""
        row = discord.ui.ActionRow()
        for item in items:
            row.add_item(item)
        self._children.append(row)
        return self

    def rows(self, *rows: discord.ui.ActionRow) -> "CardBuilder":
        for r in rows:
            self._children.append(r)
        return self

    def component(self, item: discord.ui.Item) -> "CardBuilder":
        """Добавить произвольный компонент (Section, MediaGallery и т.д.)."""
        self._children.append(item)
        return self

    # --- Сборка ---
    def build(self) -> discord.ui.Container:
        return discord.ui.Container(
            *self._children,
            accent_color=self._accent,
            spoiler=self._spoiler,
        )


def card(
    *,
    title: str | None = None,
    accent: int | None = colors.NEUTRAL,
    body: str | list[str] | None = None,
    footer: str | None = None,
    thumbnail: str | None = None,
    rows: list[discord.ui.ActionRow] | None = None,
    spoiler: bool = False,
) -> discord.ui.Container:
    """Быстрая сборка типовой карточки.

    body: строка или список строк -> один TextDisplay.
    rows: готовые ActionRow (см. components.action_row).
    """
    builder = CardBuilder(title=title, accent=accent, spoiler=spoiler)
    if thumbnail and body is None:
        builder.component(discord.ui.MediaGallery(discord.ui.MediaGalleryItem(media=thumbnail)))
    if body is not None:
        if isinstance(body, list):
            builder.lines(*body)
        else:
            builder.text(body)
    if footer:
        builder.sep()
        builder.text(f"-# {footer}")
    if rows:
        builder.rows(*rows)
    return builder.build()
