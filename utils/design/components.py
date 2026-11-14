"""Фабрики интерактивных компонентов дизайн-системы.

Кнопки и Select Menu создаются здесь и затем помещаются
в ActionRow внутри Container (см. containers.py).

Цветовые роли кнопок:
- Primary   — основное действие (blurple)
- Secondary — второстепенное действие (серый)
- Success   — подтверждение (зелёный)
- Danger    — опасное действие/удаление (красный)
"""

from __future__ import annotations

import discord


# Роли стилей
STYLE_PRIMARY = discord.ButtonStyle.primary
STYLE_SECONDARY = discord.ButtonStyle.secondary
STYLE_SUCCESS = discord.ButtonStyle.success
STYLE_DANGER = discord.ButtonStyle.danger
STYLE_LINK = discord.ButtonStyle.link

_STYLE_MAP = {
    "primary": STYLE_PRIMARY,
    "secondary": STYLE_SECONDARY,
    "success": STYLE_SUCCESS,
    "danger": STYLE_DANGER,
    "link": STYLE_LINK,
}


def btn(
    label: str,
    *,
    style: str = "secondary",
    custom_id: str | None = None,
    url: str | None = None,
    disabled: bool = False,
    row: int | None = None,
    **kwargs,
) -> discord.ui.Button:
    """Кнопка дизайн-системы.

    style: 'primary' | 'secondary' | 'success' | 'danger' | 'link'
    """
    resolved_style = _STYLE_MAP.get(style, STYLE_SECONDARY)

    button = discord.ui.Button(
        style=resolved_style,
        label=label,
        custom_id=custom_id,
        url=url,
        disabled=disabled,
        row=row,
        **kwargs,
    )
    return button


def action_row(*items: discord.ui.Item) -> discord.ui.ActionRow:
    """ActionRow для размещения внутри Container.

    Для Select Menu строка должна содержать только один select.
    """
    row = discord.ui.ActionRow()
    for item in items:
        row.add_item(item)
    return row


def select(
    options: list[discord.SelectOption],
    *,
    custom_id: str,
    placeholder: str = "Выберите…",
    min_values: int = 1,
    max_values: int = 1,
    disabled: bool = False,
    row: int | None = None,
    **kwargs,
) -> discord.ui.Select:
    """Select Menu дизайн-системы."""
    return discord.ui.Select(
        custom_id=custom_id,
        placeholder=placeholder,
        options=options,
        min_values=min_values,
        max_values=max_values,
        disabled=disabled,
        row=row,
        **kwargs,
    )


def option(
    label: str,
    value: str,
    *,
    description: str | None = None,
    default: bool = False,
) -> discord.SelectOption:
    """SelectOption."""
    return discord.SelectOption(
        label=label,
        value=value,
        description=description,
        default=default,
    )
