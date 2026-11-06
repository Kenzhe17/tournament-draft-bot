"""Components V2 дизайн-система (discord.py 2.6+).

CV2 заменяет Embed: сообщение собирается из блоков
(TextDisplay / Separator / Section+Thumbnail / MediaGallery / ActionRow)
внутри ui.LayoutView. discord.py сам ставит флаг Components V2.

Использование:
    from utils.design.cv2 import design_card

    view = design_card(
        category="success",
        title="Турнир создан",
        description="8 игроков • Single Elimination",
        kv=[("Игроки", "8/8"), ("Статус", "Регистрация открыта")],
        footer="r1z3",
    )
    await interaction.response.send_message(view=view)

Для интерактива — наследуйтесь от ui.LayoutView и добавляйте
ui.ActionRow с кнопками (см. гайд CodeZ-development/Components-v2-guide).
"""

from __future__ import annotations

import discord
from discord import ui

from utils.design.colors import _CATEGORY_COLORS, Category

FOOTER_PREFIX = "-# "


def _color(category: str) -> discord.Color:
    return _CATEGORY_COLORS.get(category, _CATEGORY_COLORS[Category.NEUTRAL])


def _md_escape(text: str) -> str:
    return str(text)


class CV2Card(ui.LayoutView):
    """Универсальная CV2-карточка дизайн-системы.

    Блоки: заголовок (##), описание, kv-секции, separator, footer (-#),
    опциональный thumbnail справа от заголовка.
    """

    def __init__(self, *, category: str = Category.NEUTRAL,
                 title: str = "", description: str | None = None,
                 kv: list[tuple[str, str]] | None = None,
                 sections: list[tuple[str, str]] | None = None,
                 thumbnail_url: str | None = None,
                 footer: str | None = None,
                 timeout: float | None = None):
        super().__init__(timeout=timeout)
        color = _color(category)
        inner: list = []

        # Заголовок (+ thumbnail если есть)
        heading_texts = [ui.TextDisplay(f"## {_md_escape(title)}")]
        if description:
            heading_texts.append(ui.TextDisplay(description))
        if thumbnail_url:
            inner.append(ui.Section(
                *heading_texts,
                accessory=ui.Thumbnail(media=thumbnail_url,
                                       description="avatar"),
            ))
        else:
            inner.extend(heading_texts)

        inner.append(ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))

        # kv-пары: "**name** — value" построчно
        if kv:
            lines = [f"**{k}** — {v}" for k, v in kv]
            inner.append(ui.TextDisplay("\n".join(lines)))
            inner.append(ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))

        # Секции с заголовками
        if sections:
            for name, value in sections:
                inner.append(ui.TextDisplay(f"**{name}**\n{value}"))
            inner.append(ui.Separator(visible=False, spacing=discord.SeparatorSpacing.small))

        if footer:
            inner.append(ui.TextDisplay(f"{FOOTER_PREFIX}{footer}"))

        # Accent-цвет карточки через Container
        self.add_item(ui.Container(*inner, accent_colour=color))
        self.accent_color = color


def design_card(**kwargs) -> CV2Card:
    """Фабрика CV2-карточки (см. CV2Card)."""
    return CV2Card(**kwargs)


class CV2ActionRow(ui.ActionRow):
    """Ряд действий для LayoutView: до 5 кнопок ИЛИ 1 select."""

    def __init__(self, *items: discord.ui.Item):
        super().__init__(*items)


def design_action_row(*buttons: discord.ui.Button) -> CV2ActionRow:
    """ActionRow из готовых кнопок (макс. 5)."""
    if len(buttons) > 5:
        raise ValueError("CV2 ActionRow: максимум 5 кнопок")
    return CV2ActionRow(*buttons)


# ---------------------------------------------------------------------------
# Конвертер Embed -> Components V2
# ---------------------------------------------------------------------------

def _embed_color(embed: discord.Embed) -> discord.Color | None:
    """Цвет embed'а как accent для Container (None -> нейтральный)."""
    c = getattr(embed, "color", None)
    if c is None:
        return None
    try:
        return discord.Color(c)
    except Exception:
        return None


def cv2_items_from_embed(embed: discord.Embed) -> list:
    """Конвертировать discord.Embed в список CV2-итемов (без Container).

    Title -> '## ', description -> TextDisplay, fields -> '**name**\\nvalue',
    footer -> '-# ', thumbnail -> Section+Thumbnail accessory.
    """
    items: list = []

    heading: list = []
    if embed.author and embed.author.name:
        heading.append(ui.TextDisplay(f"**{embed.author.name}**"))
    title = (embed.title or "").strip()
    if title:
        heading.append(ui.TextDisplay(f"## {title}"))
    if heading:
        thumb = getattr(embed, "thumbnail", None)
        thumb_url = getattr(thumb, "url", None) if thumb else None
        if thumb_url:
            items.append(ui.Section(
                *heading,
                accessory=ui.Thumbnail(media=thumb_url,
                                       description="thumbnail"),
            ))
        else:
            items.extend(heading)

    if embed.description:
        items.append(ui.TextDisplay(embed.description))

    for field in embed.fields:
        items.append(ui.TextDisplay(
            f"**{field.name}**\n{field.value}" if field.name else str(field.value)
        ))

    if embed.footer and embed.footer.text:
        items.append(ui.TextDisplay(f"-# {embed.footer.text}"))

    if getattr(embed, "image", None) and embed.image.url:
        items.append(ui.MediaGallery(discord.MediaGalleryItem(embed.image.url)))

    return items


def container_from_embed(embed: discord.Embed) -> ui.Container:
    """Container с accent-цветом embed'а и CV2-итемами его содержимого."""
    inner = cv2_items_from_embed(embed)
    color = _embed_color(embed)
    if color is not None:
        return ui.Container(*inner, accent_colour=color)
    return ui.Container(*inner)


def layout_from_embed(embed: discord.Embed) -> ui.LayoutView:
    """LayoutView-карточка из существующего embed (accent = цвет embed'а)."""
    view = ui.LayoutView(timeout=None)
    view.add_item(container_from_embed(embed))
    return view
