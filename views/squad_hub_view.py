"""Публичная карточка Squad Hub (persistent) + приватное меню.

Кнопки карточки — Button-классы с callback; тот же набор входит в
SquadHubView, который регистрируется через bot.add_view для
dispatch после рестарта.
"""

from __future__ import annotations

import logging

import discord

from services import squad_service as svc
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()


# ----------------------------------------------------------------------
# Кнопки публичной карточки
# ----------------------------------------------------------------------


class _HubFindBtn(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.primary,
                         label="Найти команду",
                         custom_id="squad:find")

    async def callback(self, interaction: discord.Interaction) -> None:
        from views.squad_panels import open_catalog
        await open_catalog(interaction)


class _HubCreateBtn(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.success,
                         label="Создать команду",
                         custom_id="squad:create")

    async def callback(self, interaction: discord.Interaction) -> None:
        from views.team_create_view import open_create_flow
        await open_create_flow(interaction)


class _HubLfgBtn(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="Ищу команду",
                         custom_id="squad:lfg")

    async def callback(self, interaction: discord.Interaction) -> None:
        from views.squad_panels import open_lfg
        await open_lfg(interaction)


class _HubMineBtn(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="Моя команда",
                         custom_id="squad:mine")

    async def callback(self, interaction: discord.Interaction) -> None:
        from views.squad_panels import open_my_team
        await open_my_team(interaction)


class _HubInvitesBtn(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.secondary,
                         label="Приглашения",
                         custom_id="squad:invites")

    async def callback(self, interaction: discord.Interaction) -> None:
        from views.squad_panels import open_invites_panel
        await open_invites_panel(interaction)


def hub_buttons() -> list[discord.ui.Button]:
    return [_HubFindBtn(), _HubCreateBtn(), _HubLfgBtn(),
            _HubMineBtn(), _HubInvitesBtn()]


class SquadHubView(discord.ui.View):
    """Persistent View — dispatch кнопок публичной карточки Squad Hub."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for b in hub_buttons():
            self.add_item(b)


# ----------------------------------------------------------------------
# Приватное меню Squad Hub (после «Назад» из каталогов)
# ----------------------------------------------------------------------


def hub_menu_view() -> discord.ui.LayoutView:
    """Эфемерное меню с теми же действиями, что и публичная карточка."""
    view = DesignView(title="r!z3 | SQUAD HUB", accent=0xA83FFF)
    view.text(
        "Найди команду. Собери состав. Играй вместе.\n"
        "-# Выберите действие:")
    view.row(*hub_buttons())
    return view


# ----------------------------------------------------------------------
# Рекрутинговая карточка команды (persistent DynamicItem)
# ----------------------------------------------------------------------

# Реэкспорт для services.squad_service.restore_hub_views
from views.team_recruit_view import (  # noqa: E402,F401
    RecruitmentCardView,
    render_recruit_card,
)
