"""Компоненты дизайн-системы: кнопки и select menu.

Цветовые роли:
  PRIMARY   — главное действие
  SECONDARY — второстепенные действия (по умолчанию)
  SUCCESS   — подтверждение
  DANGER    — удаление и опасные операции

Эмодзи передаются семантическим ключом (см. utils.design.emojis),
custom_id и callbacks существующих View не меняются.
"""

from __future__ import annotations

import enum

import discord

from utils.design.emojis import partial_emoji


class ButtonRole(enum.Enum):
    PRIMARY = discord.ButtonStyle.primary
    SECONDARY = discord.ButtonStyle.secondary
    SUCCESS = discord.ButtonStyle.success
    DANGER = discord.ButtonStyle.danger


def design_button(
    label: str,
    *,
    emoji: str | None = None,
    role: ButtonRole = ButtonRole.SECONDARY,
    custom_id: str | None = None,
    callback=None,
    url: str | None = None,
    disabled: bool = False,
) -> discord.ui.Button:
    """Кнопка дизайн-системы.

    emoji — семантический ключ ('back', 'check', 'money'...) или готовый
    unicode/custom emoji.
    """
    emoji_value = partial_emoji(emoji) if emoji else None
    # partial_emoji может вернуть unicode-строку — discord.py её тоже принимает
    return discord.ui.Button(
        label=label,
        style=role.value,
        emoji=emoji_value,
        custom_id=custom_id,
        url=url,
        disabled=disabled,
        callback=callback,
    )


def design_select(
    *,
    placeholder: str,
    options: list[dict],
    custom_id: str | None = None,
    min_values: int = 1,
    max_values: int = 1,
    row: int | None = None,
) -> discord.ui.Select:
    """Select menu дизайн-системы.

    options: [{label, value, description?, emoji?, default?}]
    emoji — семантический ключ или готовый emoji.
    """
    select_options = []
    for opt in options:
        select_options.append(
            discord.SelectOption(
                label=opt["label"],
                value=opt.get("value", opt["label"]),
                description=opt.get("description"),
                emoji=partial_emoji(opt["emoji"]) if opt.get("emoji") else None,
                default=opt.get("default", False),
            )
        )
    return discord.ui.Select(
        placeholder=placeholder,
        options=select_options,
        min_values=min_values,
        max_values=max_values,
        custom_id=custom_id,
        row=row,
    )
