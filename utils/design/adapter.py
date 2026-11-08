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
        if delegate is not None and hasattr(delegate,"interaction_check"):
            return await delegate.interaction_check(interaction)
        return True

    async def on_timeout(self) -> None:
        delegate = self._delegate
        if delegate is not None and hasattr(delegate,"on_timeout"):
            try:
                await delegate.on_timeout()
            except Exception:
                pass
        await super().on_timeout()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item | None = None,
    ) -> None:
        delegate = self._delegate
        if delegate is not None and hasattr(delegate,"on_error"):
            await delegate.on_error(interaction, error, item)
        else:
            await super().on_error(interaction, error, item)

    def is_finished(self) -> bool:
        if super().is_finished():
            return True
        delegate = self._delegate
        if delegate is not None and hasattr(delegate,"is_finished"):
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
    color = getattr(embed,"color", None)
    if color is None:
        return _DEFAULT_ACCENT
    value = color.value if hasattr(color,"value") else int(color)
    # discord.Color.default() = 0 — нейтральный акцент
    if value == 0:
        return _DEFAULT_ACCENT
    return value


# Строки-разделители внутри embed-текста (───, ===, --- и т.п.)
# разрывают секции; строки вида «**Заголовок:**» тоже начинают
# новую секцию (смысловые блоки внутри description).
_DIVIDER_RE = re.compile(r"^[\s─━═\-_=~•·]{3,}$")
_HEADER_RE = re.compile(r"^\s*\*\*[^*\n]{1,100}\*\*:?\s*$")


def _text_segments(
    content: str, split_headers: bool = False
) -> list[str | None]:
    """Разбить текст на сегменты; None = граница секции.

    split_headers=True — строка «**Заголовок:**» тоже начинает
    новый сегмент (сama строка остаётся в новом сегменте).
    """
    segments: list[str | None] = []
    block: list[str] = []
    for line in content.split("\n"):
        is_break = _DIVIDER_RE.match(line) or (
            split_headers and _HEADER_RE.match(line)
        )
        if is_break:
            text ="\n".join(block).strip("\n")
            if text.strip():
                segments.append(text)
            if _DIVIDER_RE.match(line):
                segments.append(None)
                block = []
            else:
                # Заголовок начинает новый блок, а не пустую границу
                block = [line]
            continue
        block.append(line)
    text ="\n".join(block).strip("\n")
    if text.strip():
        segments.append(text)
    # Убрать ведущие/хвостовые разделители и дубли
    while segments and segments[0] is None:
        segments.pop(0)
    while segments and segments[-1] is None:
        segments.pop()
    return segments


def _quote_block(content: str) -> list[str]:
    """Первая строка блока -> blockquote ('> '), остальные — текст
    со сдвигом на 1 пробел правее заголовка.

    Полоска ('>') есть только перед заголовком — короткий штрих
    возле него, а не на всю высоту блока. Строки текста начинаются
    на колонку правее текста заголовка (заголовок идёт после '> ',
    тело — с отступом в 3 пробела).
    """
    head = content.split("\n", 1)[0]
    if not head.strip() or head.lstrip().startswith(">"):
        return [content]
    # Полоска — только перед заголовками (**...**). Обычный текст
    # остаётся без неё, чтобы '>' не появлялся между строками.
    if not _HEADER_RE.match(head):
        return [content]
    lines = content.split("\n")
    out = ["> " + lines[0].lstrip()]
    for line in lines[1:]:
        if not line.strip():
            out.append("")
            continue
        lead = len(line) - len(line.lstrip())
        pad = max(0, 3 - lead)
        out.append(" " * pad + line)
    return ["\n".join(out)]


# Хвост «| /command» в заголовках embed'ов — убираем: команду
# показывает author-секция, дублировать в title не нужно.
_TITLE_CMD_RE = re.compile(r"\s*\|\s*/\S[^\n]*")


def strip_title_command(title: str | None) -> str | None:
    """Убрать «| /cmd» и markdown-мусор (** и пр.) из заголовка embed'а."""
    if not title:
        return title
    stripped = _TITLE_CMD_RE.sub("", title)
    stripped = stripped.replace("**","").strip(" #*_`~")
    return stripped or None


def _is_small_text(content: str) -> bool:
    """'-# ' мелкий текст (author/footer) — полоску не получает."""
    return content.lstrip().startswith("-#")


def _strip_section_head(sec: list[discord.ui.Item]) -> None:
    """Обернуть каждый текстовый блок секции в blockquote ('>' на
    каждой строке): короткая вертикальная полоска слева от блока,
    заголовок и строки под ним имеют одинаковый отступ от стены.

    Мелкий текст ('-# ' author/footer) и секции-картинки пропускаются.
    """
    for idx, item in enumerate(sec):
        if isinstance(item, discord.ui.TextDisplay):
            if _is_small_text(item.content):
                continue
            sec[idx : idx + 1] = [
                discord.ui.TextDisplay(content=p)
                for p in _quote_block(item.content)
            ]
            continue
        if isinstance(item, discord.ui.Section):
            accessory = getattr(item,"accessory", None)
            if accessory is None:
                continue
            children = list(
                getattr(item,"children", None)
                or getattr(item,"_children", [])
            )
            new_children: list[discord.ui.Item] = []
            changed = False
            for c in children:
                if isinstance(c, discord.ui.TextDisplay) and not _is_small_text(
                    c.content
                ):
                    new_children.extend(
                        discord.ui.TextDisplay(content=p)
                        for p in _quote_block(c.content)
                    )
                    changed = True
                else:
                    new_children.append(c)
            if not changed:
                continue
            # Section вмещает максимум 3 TextDisplay
            if sum(
                isinstance(c, discord.ui.TextDisplay) for c in new_children
            ) > 3:
                continue
            sec[idx] = discord.ui.Section(
                *new_children, accessory=accessory
            )
            continue
        # MediaGallery/Separator и пр. — пропускаем


def embed_to_section_items(
    embed: discord.Embed,
) -> list[list[discord.ui.Item]]:
    """Embed -> список секций, каждая = список компонентов.

    Секции — смысловые блоки внутри ЕДИНОЙ карточки:
      - author + title + description (до первого разделителя) -> секция 1
      - строка-разделитель (───, === и т.п.) внутри текста -> граница секции
      - строка «**Заголовок:**» в description -> граница секции
      - каждое непустое поле -> своя секция; подряд идущие inline-поля
        группируются в одну секцию
      - image -> своя секция (вместе с footer, если он есть)
      - footer/timestamp -> в конец последней текстовой секции
      - между секциями — тонкий Separator; первая строка каждой
        секции получает короткую '>' полоску
    """
    sections: list[list[discord.ui.Item]] = []
    current: list[discord.ui.Item] = []

    def _flush() -> None:
        nonlocal current
        if current:
            sections.append(current)
            current = []

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
        current.append(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=thumb_url)
            )
        )
        thumb_url = None

    def _emit_text(content: str, split_headers: bool = False) -> None:
        nonlocal thumb_used
        first = True
        for segment in _text_segments(content, split_headers=split_headers):
            if segment is None:
                _flush()
                first = True
                continue
            if split_headers and not first:
                # Каждый смысловой блок description — своя секция
                _flush()
            first = False
            for chunk in chunk_text(segment):
                if thumb_url and not thumb_used:
                    current.append(
                        discord.ui.Section(
                            discord.ui.TextDisplay(content=chunk),
                            accessory=discord.ui.Thumbnail(media=thumb_url),
                        )
                    )
                    thumb_used = True
                else:
                    current.append(discord.ui.TextDisplay(content=chunk))

    # Author — с иконкой (иконка бота/контекста) как Section-аксессуар
    if embed.author and embed.author.name:
        author_text = f"-# {embed.author.name}"
        if embed.author.icon_url:
            current.append(
                discord.ui.Section(
                    discord.ui.TextDisplay(content=author_text),
                    accessory=discord.ui.Thumbnail(media=embed.author.icon_url),
                )
            )
        else:
            _emit_text(author_text)

    # Title
    title_text = strip_title_command(embed.title)
    if title_text:
        _emit_text(f"## {title_text}")
        if embed.description:
            current.append(separator())

    # Description — строка «**Заголовок:**» начинает новую секцию
    if embed.description:
        _emit_text(embed.description, split_headers=True)

    # Fields: непустые поля — отдельные секции; подряд идущие
    # inline-поля собираются в одну секцию. Поля-заглушки
    # (только \u200b/пробелы) пропускаются — иначе получаются
    # пустые карточки.
    def _field_blank(name: str, value: str) -> bool:
        return not (name + value).translate({0x200B: None, 0x200C: None, 0x200D: None, 0xFEFF: None}).strip()


    i = 0
    fields = embed.fields
    while i < len(fields):
        field = fields[i]
        name = field.name or""
        value = field.value or""
        if _field_blank(name, value):
            i += 1
            continue
        if field.inline:
            group: list[str] = []
            while i < len(fields) and fields[i].inline:
                gf = fields[i]
                gname = gf.name or""
                gvalue = gf.value or""
                if not _field_blank(gname, gvalue):
                    group.append(f"**{gname}**\n{gvalue}"if gname else gvalue)
                i += 1
            _flush()
            if group:
                _emit_text("\n\n".join(group))
            continue
        _flush()
        _emit_text(f"**{name}**\n{value}"if name else value)
        i += 1

    # Image — отдельная секция
    if embed.image and embed.image.url:
        _flush()
        current.append(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=embed.image.url)
            )
        )

    # Footer/timestamp — в конец текущей (последней) секции
    footer_items: list[discord.ui.Item] = []
    if embed.footer and embed.footer.text:
        footer_text = embed.footer.text
        if embed.timestamp:
            footer_text = f"{footer_text} • <t:{int(embed.timestamp.timestamp())}:R>"
        footer_items.append(separator())
        footer_items.append(
            discord.ui.TextDisplay(content=f"-# {footer_text}")
        )
    elif embed.timestamp:
        footer_items.append(separator())
        footer_items.append(
            discord.ui.TextDisplay(
                content=f"-# <t:{int(embed.timestamp.timestamp())}:R>"
            )
        )
    if footer_items:
        # Не оставлять footer одиноким в карточке с одной картинкой
        # не будем: он логично завершает секцию с image.
        current.extend(footer_items)

    _flush()

    # Сливаем хвост при большом числе секций — ограничивает количество
    # Separator/'>' полосок и общий счёт компонентов (~40 на сообщение).
    while len(sections) > 10:
        sections[-2].append(separator())
        sections[-2].extend(sections[-1])
        sections.pop()

    # Тонкий Separator в конце каждой секции, кроме последней —
    # визуальное разделение смысловых блоков.
    for sec in sections[:-1]:
        if sec and not isinstance(sec[-1], discord.ui.Separator):
            sec.append(separator())

    # Короткая '>' полоска слева от первой строки каждой секции
    # (аналог короткого accent-бара, но только у заголовка/блока).
    for sec in sections:
        _strip_section_head(sec)

    return sections or [[]]


def embed_to_container(embed: discord.Embed) -> discord.ui.Container:
    """Embed -> ОДИН Container: все секции внутри единой карточки."""
    container = discord.ui.Container()
    for sec in embed_to_section_items(embed):
        for item in sec:
            container.add_item(item)
    return container


def embed_to_sections(
    embed: discord.Embed,
) -> list[discord.ui.Container]:
    """Embed -> Container на каждую секцию (совместимость, не используется
    в compose — там всё собирается в одну карточку)."""
    containers: list[discord.ui.Container] = []
    for sec in embed_to_section_items(embed):
        container = discord.ui.Container()
        for item in sec:
            container.add_item(item)
        containers.append(container)
    return containers or [discord.ui.Container()]


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

    # Embed -> ОДНА карточка: все смысловые секции внутри единого
    # Container (сепараторы и '>' полоски — внутри); кнопки — в конец.
    if embed is not None:
        container = embed_to_container(embed)
        sections = [container]
    else:
        sections = [discord.ui.Container()]

    delegate = view if isinstance(view, discord.ui.View) else None
    layout = _ComposedLayoutView(
        delegate=delegate,
        timeout=getattr(view,"timeout", 180.0),
    )
    for container in sections:
        layout.add_item(container)
    target = sections[-1]
    if text:
        # Текст (бывший content=) идёт в конец последней секции
        target.add_item(discord.ui.TextDisplay(content=text))
        target.add_item(separator())
    if view is not None:
        # Discord рендерит внутри Container только ОДИН ActionRow —
        # первый ряд оставляем в карточке, остальные выносим на
        # верхний уровень LayoutView (обычные ряды кнопок под карточкой).
        for i, row in enumerate(_view_rows(view)):
            (target if i == 0 else layout).add_item(row)
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

    Используется для правок вида'edit_message(view=...)'без embed —
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
    # Контейнеры, опустевшие после удаления рядов (обёртки кнопок),
    # убираем целиком — иначе остаются пустые карточки.
    for container in list(layout.children):
        if isinstance(container, discord.ui.Container):
            for child in list(container.children):
                if isinstance(child, discord.ui.ActionRow):
                    container.remove_item(child)
            if not container.children:
                layout.remove_item(container)
        elif isinstance(container, discord.ui.ActionRow):
            layout.remove_item(container)

    if view is not None:
        rows = _interactive_rows(view)
        target = None
        for child in layout.children:
            if isinstance(child, discord.ui.Container):
                target = child # последний Container — ряды в конец карточки
        if target is None:
            target = discord.ui.Container()
            layout.add_item(target)
        # Первый ряд — внутрь карточки; Discord рендерит внутри
        # Container только один ActionRow, остальные — сверху.
        for i, row in enumerate(rows):
            (target if i == 0 else layout).add_item(row)

    return layout


def _is_v2_message(message: discord.Message) -> bool:
    """Проверить, что сообщение отправлено с флагом IS_COMPONENTS_V2."""
    flags = getattr(message,"flags", None)
    if flags is None:
        return False
    value = getattr(flags,"value", flags)
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
