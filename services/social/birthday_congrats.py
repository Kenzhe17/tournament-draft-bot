"""V2-карточка автоматического поздравления с днём рождения."""

from __future__ import annotations

import discord

from services.social.common import (
    BANNER_BIRTHDAY_CONGRATS, banner_file, social_view,
)


def build_congrats_view(member: discord.Member) -> discord.ui.LayoutView:
    """Персональное поздравление имениннику."""
    view = social_view(BANNER_BIRTHDAY_CONGRATS, timeout=None)
    view.text(
        f"**С днём рождения, {member.mention}!**\n"
        "Сегодня особенный день — твой день рождения!"
    )
    view.sep()
    view.text(
        "От всего сообщества **r!z3** желаем ярких моментов, "
        "хорошего настроения и незабываемых побед."
    )
    view.sep()
    view.text("*Пусть этот год принесёт тебе много приятных воспоминаний.*")
    return view


def congrats_file() -> discord.File:
    return banner_file(BANNER_BIRTHDAY_CONGRATS)
