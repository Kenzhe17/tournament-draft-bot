"""Общие app_commands.Group для реорганизованной структуры команд.

Группы регистрируются вручную в bot.py (tree.add_command),
а self-инжекция в подкоманды делается через cmd.binding = cog
в cog_load каждого cog'а (см. bind_commands helper).
"""

from __future__ import annotations

from typing import Iterable

from discord import app_commands

player_group = app_commands.Group(
    name="player", description="Профиль, история, ранг и рейтинг"
)
player_set_group = app_commands.Group(
    name="set", description="Изменить поля профиля"
)
player_group.add_command(player_set_group)
economy_group = app_commands.Group(
    name="economy", description="Баланс, переводы, магазин-сейф и ставки"
)
games_group = app_commands.Group(
    name="games", description="Мини-игры на монеты"
)
crime_group = app_commands.Group(
    name="crime", description="Криминальные действия"
)
admin_group = app_commands.Group(
    name="admin", description="Административные операции"
)
server_group = app_commands.Group(
    name="server", description="Настройки сервера"
)
reputation_group = app_commands.Group(
    name="reputation", description="Репутация: профиль, история, статистика"
)
tag_group = app_commands.Group(
    name="tag", description="Награды за серверный тег"
)

# Группы, регистрируемые в корне дерева (bot.py)
ROOT_GROUPS = (
    player_group,
    economy_group,
    games_group,
    crime_group,
    admin_group,
    server_group,
    reputation_group,
    tag_group,
)


def bind_commands(group: app_commands.Group, cog, names: Iterable[str]) -> None:
    """Привязать подкоманды группы к экземпляру cog (инжекция self)."""
    wanted = set(names)
    for cmd in group.walk_commands():
        if isinstance(cmd, app_commands.Group):
            continue
        if cmd.name in wanted:
            cmd.binding = cog
