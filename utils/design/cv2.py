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
