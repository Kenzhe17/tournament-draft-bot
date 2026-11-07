"""Адаптер классических discord.Embed -> Components V2 Container.

Позволяет мигрировать существующие embed-билдеры без переписывания
всей логики: embed превращается в Container с TextDisplay/Section/
Separator/MediaGallery, а кнопки исходного View перемещаются
в ActionRow внутри того же Container.

ВАЖНО: это промежуточный слой миграции. Новые интерфейсы
следует собирать напрямую через DesignView/CardBuilder.
"""

from __future__ import annotations

import discord

from utils.design import colors
from utils.design.containers import chunk_text, section, separator

# Маппинг discord.Color -> accent_color Container'а.
# Популярные цвета Embed сопоставляются с палитрой дизайн-системы.
_COLOR_MAP = {
    0x2ECC71: colors.SUCCESS,
    0xE74C3C: colors.ERROR,
    0xF1C40F: colors.WARNING,
    0x5865F2: colors.INFO,
    0xF0B232: colors.ECONOMY,
    0x747F8D: colors.NEUTRAL,
    0x232428: colors.NEUTRAL,
    0x99AAB5: colors.NEUTRAL,
    0x43B581: colors.SUCCESS,
    0xF04747: colors.ERROR,
    0xFAA61A: colors.WARNING,
    0x7289DA: colors.INFO,
    0xFEE75C: colors.WARNING,
    0xED4245: colors.ERROR,
    0x57F287: colors.SUCCESS,
    0x5865F2: colors.PRIMARY,
    0x8E44AD: colors.TOURNAMENT,
    0x3498DB: colors.DRAFT,
    0xE67E22: colors.MATCH,
    0x1ABC9C: colors.PROFILE,
    0xF39C12: colors.RATING,
    0x9B59B6: colors.GAME,
    0xC0392B: colors.MODERATION,
    0x00B0F4: colors.INFO,
    0xFF73FA: colors.GAME,
    0xE91E63: colors.MODERATION,
    0x4CAF50: colors.SUCCESS,
    0xFF9800: colors.WARNING,
    0x2196F3: colors.INFO,
    0x9C27B0: colors.GAME,
    0x607D8B: colors.NEUTRAL,
}

_DEFAULT_ACCENT = colors.NEUTRAL


def _accent_from_embed(embed: discord.Embed) -> int:
    """Цвет Embed -> accent_color Container."""
    color = getattr(embed, "color", None)
    if color is None:
        return _DEFAULT_ACCENT
    value = color.value if hasattr(color, "value") else int(color)
    # discord.Color.value — int; берём как есть для точного совпадения
    return _COLOR_MAP.get(value, _DEFAULT_ACCENT)


def _add_text(container: discord.ui.Container, content: str) -> None:
    """Добавить текст с разбивкой на TextDisplay."""
    for chunk in chunk_text(content):
        container.add_item(discord.ui.TextDisplay(content=chunk))


def embed_to_container(embed: discord.Embed) -> discord.ui.Container:
    """Конвертировать discord.Embed в discord.ui.Container.

    Структура:
      - author -> TextDisplay (мелкий)
      - title  -> '# title'
      - description -> TextDisplay
      - thumbnail -> MediaGallery (первым, если есть)
      - fields -> 'name\\nvalue' блоки через Separator
      - image -> MediaGallery
      - footer -> '-# footer • timestamp'
    """
    container = discord.ui.Container(accent_color=_accent_from_embed(embed))

    # Thumbnail — в начало как MediaGallery
    if embed.thumbnail and embed.thumbnail.url:
        container.add_item(
            discord.ui.MediaGallery(
                discord.ui.MediaGalleryItem(media=embed.thumbnail.url)
            )
        )

    # Author
    if embed.author and embed.author.name:
        author_text = f"-# {embed.author.name}"
        if embed.author.icon_url:
            # icon_url в TextDisplay не поддерживается — пропускаем
            pass
        container.add_item(discord.ui.TextDisplay(content=author_text))

    # Title
    if embed.title:
        _add_text(container, f"# {embed.title}")
        container.add_item(separator())

    # Description
    if embed.description:
        _add_text(container, embed.description)

    # Fields
    for i, field in enumerate(embed.fields):
        if i > 0 or embed.description:
            container.add_item(separator())
        name = field.name or ""
        value = field.value or ""
        block = f"**{name}**\n{value}" if name else value
        _add_text(container, block)

    # Image
    if embed.image and embed.image.url:
        container.add_item(separator())
        container.add_item(
            discord.ui.MediaGallery(
                discord.ui.MediaGalleryItem(media=embed.image.url)
            )
        )

    # Footer
    if embed.footer and embed.footer.text:
        container.add_item(separator())
        footer_text = embed.footer.text
        if embed.timestamp:
            footer_text = f"{footer_text} • <t:{int(embed.timestamp.timestamp())}:R>"
        container.add_item(discord.ui.TextDisplay(content=f"-# {footer_text}"))
    elif embed.timestamp:
        container.add_item(separator())
        container.add_item(
            discord.ui.TextDisplay(
                content=f"-# <t:{int(embed.timestamp.timestamp())}:R>"
            )
        )

    return container


def _view_rows(view: discord.ui.View) -> list[discord.ui.ActionRow]:
    """Извлечь ActionRow из items обычного View (по row-атрибуту)."""
    rows_map: dict[int, discord.ui.ActionRow] = {}
    for item in view.children:
        if not hasattr(item, "custom_id") and not isinstance(
            item, (discord.ui.Button, discord.ui.Select)
        ):
            continue
        row_idx = getattr(item, "row", None)
        if row_idx is None:
            row_idx = len(rows_map)
        if row_idx not in rows_map:
            rows_map[row_idx] = discord.ui.ActionRow()
        rows_map[row_idx].add_item(item)
    return [rows_map[k] for k in sorted(rows_map)]


def compose(
    embed: discord.Embed | None = None,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    *,
    accent: int | None = None,
) -> discord.ui.LayoutView:
    """Собрать LayoutView из Embed и/или View.

    - embed -> Container с контентом
    - view (обычный View) -> его кнопки/select попадают в ActionRow
      внутри того же Container (одна цельная карточка)
    - view (LayoutView/DesignView) -> используется как есть, embed
      добавляется отдельным Container сверху (редкий случай)
    """
    if isinstance(view, discord.ui.LayoutView):
        # Уже V2 — возвращаем как есть (embed игнорируем: V2 не может
        # содержать embed; вызывающий код должен мигрировать view)
        return view

    container = (
        embed_to_container(embed)
        if embed is not None
        else discord.ui.Container(accent_color=accent or colors.NEUTRAL)
    )

    if view is not None:
        for row in _view_rows(view):
            container.add_item(row)

    layout = discord.ui.LayoutView(
        timeout=getattr(view, "timeout", 180.0)
    )
    layout.add_item(container)
    return layout
