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
    """Цвет Embed -> accent_color Container.

    Используется фактический цвет Embed — он становится акцентной
    полосой Container (нативный аналог цветной полосы Embed).
    """
    color = getattr(embed, "color", None)
    if color is None:
        return _DEFAULT_ACCENT
    value = color.value if hasattr(color, "value") else int(color)
    # discord.Color.default() = 0 — нейтральный акцент
    if value == 0:
        return _DEFAULT_ACCENT
    return value


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
    """Извлечь ActionRow из items обычного View (по row-атрибуту).

    Select Menu всегда получает отдельный ряд — Discord не позволяет
    смешивать select с кнопками в одном ActionRow.
    """
    rows_map: dict[int, discord.ui.ActionRow] = {}
    select_rows: list[discord.ui.ActionRow] = []
    for item in view.children:
        if not hasattr(item, "custom_id") and not isinstance(
            item, (discord.ui.Button, discord.ui.Select)
        ):
            continue
        if isinstance(item, discord.ui.Select):
            row = discord.ui.ActionRow()
            row.add_item(item)
            select_rows.append(row)
            continue
        row_idx = getattr(item, "row", None)
        if row_idx is None:
            row_idx = len(rows_map)
        if row_idx not in rows_map:
            rows_map[row_idx] = discord.ui.ActionRow()
        rows_map[row_idx].add_item(item)
    return [rows_map[k] for k in sorted(rows_map)] + select_rows


def compose(
    embed: discord.Embed | None = None,
    view: discord.ui.View | discord.ui.LayoutView | None = None,
    *,
    accent: int | None = None,
    text: str | None = None,
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

    if text:
        # Текст (бывший content=) идёт первым блоком карточки
        container.add_item(discord.ui.TextDisplay(content=text))
        container.add_item(separator())

    if view is not None:
        for row in _view_rows(view):
            container.add_item(row)

    layout = discord.ui.LayoutView(
        timeout=getattr(view, "timeout", 180.0)
    )
    layout.add_item(container)
    return layout


def text_card(
    content: str,
    *,
    accent: int | None = None,
    title: str | None = None,
    timeout: float | None = None,
) -> discord.ui.LayoutView:
    """V2-карточка из простого текста (замена content= для V2-сообщений)."""
    from utils.design.layouts import DesignView

    view = DesignView(
        title=title,
        accent=accent if accent is not None else _DEFAULT_ACCENT,
        timeout=timeout if timeout is not None else 180.0,
    )
    view.text(content)
    return view


def _interactive_rows(view) -> list[discord.ui.ActionRow]:
    """ActionRow'ы из обычного View или DesignView/LayoutView."""
    rows: list[discord.ui.ActionRow] = []
    if isinstance(view, discord.ui.LayoutView):
        # В LayoutView ищем ActionRow внутри Container'ов
        for child in view.walk_children():
            if isinstance(child, discord.ui.ActionRow):
                rows.append(child)
        return rows
    return _view_rows(view)


def swap_view(
    message: discord.Message,
    view: discord.ui.View | discord.ui.LayoutView | None,
) -> discord.ui.LayoutView:
    """Построить LayoutView на основе СУЩЕСТВУЮЩЕГО V2-сообщения,
    заменив только интерактивные ряды (кнопки/select).

    Используется для правок вида 'edit_message(view=...)' без embed —
    текст карточки сохраняется, кнопки обновляются.
    view=None — убрать все кнопки.
    """
    layout = discord.ui.LayoutView.from_message(message)

    # Удаляем старые ActionRow из всех Container'ов верхнего уровня
    for container in layout.children:
        if not isinstance(container, discord.ui.Container):
            continue
        for child in list(container.children):
            if isinstance(child, discord.ui.ActionRow):
                container.remove_item(child)

    if view is not None:
        for row in _interactive_rows(view):
            # Добавляем в первый Container (или создаём новый)
            target = next(
                (c for c in layout.children if isinstance(c, discord.ui.Container)),
                None,
            )
            if target is None:
                target = discord.ui.Container()
                layout.add_item(target)
            target.add_item(row)

    return layout


def _is_v2_message(message: discord.Message) -> bool:
    """Проверить, что сообщение отправлено с флагом IS_COMPONENTS_V2."""
    flags = getattr(message, "flags", None)
    if flags is None:
        return False
    value = getattr(flags, "value", flags)
    try:
        return bool(int(value) & (1 << 15))
    except (TypeError, ValueError):
        return False


def smart_view(
    message: discord.Message,
    view: discord.ui.View | discord.ui.LayoutView | None,
):
    """Подобрать объект для edit(view=...) под тип существующего сообщения.

    - V2-сообщение -> LayoutView с сохранённым текстом и новыми кнопками
      (swap_view); view=None -> кнопки убираются, карточка остаётся.
    - классическое сообщение -> view возвращается как есть
      (None -> очистка компонентов).
    """
    if not _is_v2_message(message):
        return view
    return swap_view(message, view)
