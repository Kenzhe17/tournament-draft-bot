"""Адаптер классических discord.Embed -> Components V2 Container.

Позволяет мигрировать существующие embed-билдеры без переписывания
всей логики: embed превращается в Container с TextDisplay/Section/
Separator/MediaGallery, а кнопки исходного View перемещаются
в ActionRow внутри того же Container.

ВАЖНО: это промежуточный слой миграции. Новые интерфейсы
следует собирать напрямую через DesignView/CardBuilder.
"""

from __future__ import annotations

import re

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

# Discord CV2: Container вмещает максимум 10 дочерних компонентов.
_CONTAINER_MAX_CHILDREN = 10


def _split_container(
    container: discord.ui.Container,
) -> list[discord.ui.Container]:
    """Разбить Container на части по <=10 children (лимит Discord CV2).

    Разбивка сохраняет порядок children и акцентную полосу
    на всех частях — визуально это одна логическая карточка.
    """
    children = list(container.children)
    if len(children) <= _CONTAINER_MAX_CHILDREN:
        return [container]

    accent = container.accent_color
    spoiler = getattr(container, "spoiler", False)
    parts: list[discord.ui.Container] = []
    for i in range(0, len(children), _CONTAINER_MAX_CHILDREN):
        chunk = children[i : i + _CONTAINER_MAX_CHILDREN]
        part = discord.ui.Container(
            accent_color=accent,
            spoiler=spoiler,
        )
        for child in chunk:
            container.remove_item(child)
            part.add_item(child)
        parts.append(part)
    return parts


class _ComposedLayoutView(discord.ui.LayoutView):
    """LayoutView-обёртка с делегированием к исходному View.

    compose() переносит кнопки обычного discord.ui.View в Container,
    но обработчики уровня View (interaction_check, on_timeout,
    on_error, is_finished) должны сохраниться — поэтому LayoutView
    проксирует их на исходный view-объект.
    """

    def __init__(self, delegate=None, **kwargs):
        super().__init__(**kwargs)
        self._delegate = delegate

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        delegate = self._delegate
        if delegate is not None and hasattr(delegate, "interaction_check"):
            return await delegate.interaction_check(interaction)
        return True

    async def on_timeout(self) -> None:
        delegate = self._delegate
        if delegate is not None and hasattr(delegate, "on_timeout"):
            try:
                await delegate.on_timeout()
            except Exception:
                pass
        await super().on_timeout()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        delegate = self._delegate
        if delegate is not None and hasattr(delegate, "on_error"):
            await delegate.on_error(interaction, error)
        else:
            await super().on_error(interaction, error)

    def is_finished(self) -> bool:
        if super().is_finished():
            return True
        delegate = self._delegate
        if delegate is not None and hasattr(delegate, "is_finished"):
            try:
                return bool(delegate.is_finished())
            except Exception:
                return False
        return False


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


# Строки-разделители внутри embed-текста (───, ===, --- и т.п.)
# превращаем в нативный discord.ui.Separator, а не текст.
_DIVIDER_RE = re.compile(r"^[\s─━═\-_=~•·]{3,}$")


def _text_segments(content: str) -> list[str | None]:
    """Разбить текст на сегменты; None = разделитель."""
    segments: list[str | None] = []
    block: list[str] = []
    for line in content.split("\n"):
        if _DIVIDER_RE.match(line):
            text = "\n".join(block).strip("\n")
            if text.strip():
                segments.append(text)
            block = []
            segments.append(None)
        else:
            block.append(line)
    text = "\n".join(block).strip("\n")
    if text.strip():
        segments.append(text)
    # Убрать ведущие/хвостовые разделители и дубли
    while segments and segments[0] is None:
        segments.pop(0)
    while segments and segments[-1] is None:
        segments.pop()
    return segments


def _add_text(container: discord.ui.Container, content: str) -> None:
    """Добавить текст с разбивкой на TextDisplay и Separator."""
    for segment in _text_segments(content):
        if segment is None:
            container.add_item(separator())
            continue
        for chunk in chunk_text(segment):
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

    # Thumbnail — маленькая картинка справа от первого текстового блока
    # (Section accessory), как в классическом Embed. MediaGallery не
    # используем: он рендерит картинку во всю ширину карточки.
    thumb_url = (
        embed.thumbnail.url
        if embed.thumbnail and embed.thumbnail.url
        else None
    )
    thumb_used = False

    # attachment:// (локальные файлы, например картинки питомцев) не
    # резолвится в Section-аксессуаре — оставляем MediaGallery.
    if thumb_url and thumb_url.startswith("attachment://"):
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=thumb_url)
            )
        )
        thumb_url = None

    def _add_text_maybe_thumb(content: str) -> None:
        nonlocal thumb_used
        segments = _text_segments(content)
        for i, segment in enumerate(segments):
            if segment is None:
                container.add_item(separator())
                continue
            chunks = chunk_text(segment)
            if thumb_url and not thumb_used and chunks:
                container.add_item(
                    discord.ui.Section(
                        discord.ui.TextDisplay(content=chunks[0]),
                        accessory=discord.ui.Thumbnail(media=thumb_url),
                    )
                )
                thumb_used = True
                for chunk in chunks[1:]:
                    container.add_item(discord.ui.TextDisplay(content=chunk))
            else:
                for chunk in chunks:
                    container.add_item(discord.ui.TextDisplay(content=chunk))

    # Author — с иконкой (иконка бота/контекста) как Section-аксессуар
    if embed.author and embed.author.name:
        author_text = f"-# {embed.author.name}"
        if embed.author.icon_url:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(content=author_text),
                    accessory=discord.ui.Thumbnail(media=embed.author.icon_url),
                )
            )
        else:
            _add_text_maybe_thumb(author_text)

    # Title
    if embed.title:
        title_text = f"## {embed.title}"
        if thumb_url and not thumb_used:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(content=title_text),
                    accessory=discord.ui.Thumbnail(media=thumb_url),
                )
            )
            thumb_used = True
        else:
            _add_text(container, title_text)
        container.add_item(separator())

    # Description
    if embed.description:
        _add_text_maybe_thumb(embed.description)

    # Fields
    for i, field in enumerate(embed.fields):
        if i > 0 or embed.description:
            container.add_item(separator())
        name = field.name or ""
        value = field.value or ""
        block = f"**{name}**\n{value}" if name else value
        _add_text_maybe_thumb(block)

    # Image
    if embed.image and embed.image.url:
        container.add_item(separator())
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=embed.image.url)
            )
        )

    # Footer
    if embed.footer and embed.footer.text:
        container.add_item(separator())
        footer_text = embed.footer.text
        if embed.timestamp:
            footer_text = f"{footer_text} • <t:{int(embed.timestamp.timestamp())}:R>"
        _add_text_maybe_thumb(f"-# {footer_text}")
    elif embed.timestamp:
        container.add_item(separator())
        container.add_item(
            discord.ui.TextDisplay(
                content=f"-# <t:{int(embed.timestamp.timestamp())}:R>"
            )
        )

    return container


def _view_rows(view: discord.ui.View) -> list[discord.ui.ActionRow]:
    """Извлечь ActionRow из items обычного View.

    Кнопки без явного row упаковываются в общие ряды по 5 штук
    (как в классическом View), Select Menu всегда получает
    отдельный ряд в своей позиции — Discord не позволяет смешивать
    select с кнопками в одном ActionRow.
    """
    rows: list[discord.ui.ActionRow] = []
    auto_row: discord.ui.ActionRow | None = None
    auto_count = 0

    def _flush() -> None:
        nonlocal auto_row, auto_count
        if auto_row is not None:
            rows.append(auto_row)
        auto_row = None
        auto_count = 0

    for item in view.children:
        if isinstance(item, discord.ui.Select):
            _flush()
            row = discord.ui.ActionRow()
            row.add_item(item)
            rows.append(row)
            continue
        if not isinstance(item, discord.ui.Button):
            continue
        if auto_row is None or auto_count >= 5:
            auto_row = discord.ui.ActionRow()
            auto_count = 0
        auto_row.add_item(item)
        auto_count += 1
    _flush()
    return rows


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

    delegate = view if isinstance(view, discord.ui.View) else None
    layout = _ComposedLayoutView(
        delegate=delegate,
        timeout=getattr(view, "timeout", 180.0),
    )
    parts = _split_container(container)
    accent_color = getattr(parts[0], "accent_color", None) if parts else None
    for part in parts:
        layout.add_item(part)
    if view is not None:
        # Каждый ActionRow — в собственном Container: Discord CV2
        # надёжно рендерит только один ActionRow внутри Container
        # (при двух рядах в одном контейнере первый пропадал), а голый
        # ActionRow на верхнем уровне выглядит как кнопки «снаружи».
        for row in _view_rows(view):
            btn_box = discord.ui.Container(accent_color=accent_color)
            btn_box.add_item(row)
            layout.add_item(btn_box)
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
    if not isinstance(layout, _ComposedLayoutView):
        # from_message возвращает чистый LayoutView — оборачиваем,
        # чтобы сохранить делегирование interactive-логики
        delegate = (
            view
            if isinstance(view, discord.ui.View)
            and not isinstance(view, discord.ui.LayoutView)
            else None
        )
        wrapped = _ComposedLayoutView(
            delegate=delegate, timeout=layout.timeout
        )
        for child in layout.children:
            wrapped.add_item(child)
        layout = wrapped

    # Удаляем старые ActionRow — и из Container'ов, и с верхнего уровня.
    # Пустые Container'ы, в которых был только ActionRow (ряды кнопок),
    # удаляем целиком, иначе останутся пустые карточки.
    for container in list(layout.children):
        if isinstance(container, discord.ui.Container):
            had_only_rows = bool(container.children) and all(
                isinstance(c, discord.ui.ActionRow) for c in container.children
            )
            for child in list(container.children):
                if isinstance(child, discord.ui.ActionRow):
                    container.remove_item(child)
            if had_only_rows or not container.children:
                layout.remove_item(container)
        elif isinstance(container, discord.ui.ActionRow):
            layout.remove_item(container)

    if view is not None:
        rows = _interactive_rows(view)
        accent = None
        for child in layout.children:
            if isinstance(child, discord.ui.Container):
                accent = getattr(child, "accent_color", None)
                break
        # Каждый ActionRow — в собственном Container (см. compose):
        # внутри одного Container Discord показывает только один ряд.
        for row in rows:
            btn_box = discord.ui.Container(accent_color=accent)
            btn_box.add_item(row)
            layout.add_item(btn_box)

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
