"""AGENT 04 / PR #39: owner-only проверки админ-команд.

Для каждой команды, переведённой на is_bot_owner(), проверяем
server-side check-предикаты app_commands:
- BOT OWNER → разрешено;
- Discord ADMIN → запрещено;
- организатор/обычный пользователь → запрещено.

Плюс регрессия: /reputation stats остаётся публичной.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OWNER_ID = 999001

OWNER_ONLY_COMMANDS = [
    ("reputation", "adjust"),
    ("reputation", "history"),
    ("reputation", "invites_debug"),
    ("banner", "preview"),
    ("banner", "status"),
    ("banner", "leaderboard"),
]


def _interaction(user_id: int, *, admin: bool = False):
    return SimpleNamespace(
        user=SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(administrator=admin),
        ),
        guild_id=1,
        channel_id=100,
    )


@pytest.fixture
def groups(monkeypatch):
    import utils.permissions as perms
    from cogs.groups import reputation_group, banner_group
    from cogs.reputation import ReputationCog
    from cogs.banner_admin import BannerAdminCog

    monkeypatch.setattr(perms, "BOT_OWNER_ID", OWNER_ID)
    # Коги биндят команды в группы при __init__ — создаём инстансы,
    # чтобы get_command() вернул актуальные Command-объекты.
    ReputationCog(SimpleNamespace())
    BannerAdminCog(SimpleNamespace())
    return {"reputation": reputation_group, "banner": banner_group}


def _command(groups, group_name, cmd_name):
    return groups[group_name].get_command(cmd_name)


@pytest.mark.asyncio
@pytest.mark.parametrize("group_name,cmd_name", OWNER_ONLY_COMMANDS)
async def test_owner_allowed(groups, group_name, cmd_name):
    cmd = _command(groups, group_name, cmd_name)
    assert cmd is not None, f"{group_name} {cmd_name} не найдена"
    assert cmd.checks, f"{group_name} {cmd_name} без проверок!"
    for check in cmd.checks:
        assert await check(_interaction(OWNER_ID)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("group_name,cmd_name", OWNER_ONLY_COMMANDS)
async def test_admin_denied(groups, group_name, cmd_name):
    """Discord administrator НЕ проходит owner-only check."""
    cmd = _command(groups, group_name, cmd_name)
    denied = False
    for check in cmd.checks:
        try:
            result = await check(_interaction(555, admin=True))
            if result is False:
                denied = True
        except Exception:
            denied = True
    assert denied, f"{group_name} {cmd_name} пропустила админа"


@pytest.mark.asyncio
@pytest.mark.parametrize("group_name,cmd_name", OWNER_ONLY_COMMANDS)
async def test_regular_user_denied(groups, group_name, cmd_name):
    cmd = _command(groups, group_name, cmd_name)
    denied = False
    for check in cmd.checks:
        try:
            result = await check(_interaction(777))
            if result is False:
                denied = True
        except Exception:
            denied = True
    assert denied, f"{group_name} {cmd_name} пропустила обычного юзера"


@pytest.mark.asyncio
async def test_rep_stats_stays_public(groups):
    """/reputation stats — публичная команда без owner-only gate."""
    cmd = _command(groups, "reputation", "stats")
    assert cmd is not None
    # Публичная: либо нет проверок, либо все пропускают обычного юзера.
    for check in cmd.checks:
        result = await check(_interaction(777))
        assert result is not False
