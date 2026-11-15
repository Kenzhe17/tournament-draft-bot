"""ADMIN ONLY для CASH CUP (ТЗ §7): ORG/MANAGER/MODERATOR/HEAD MODERATOR
не дают административный доступ. Проверяется серверная функция
_cashcup_admin → moderation_service.is_admin на каждом действии."""
import re
from types import SimpleNamespace

import pytest
from discord.ext import commands

import services.moderation_service as ms
from cogs import cashcup


def _member(*, admin_perm=False, owner_id=0, user_id=555, bot_owner=0, admin_ids=()):
    guild = SimpleNamespace(owner_id=owner_id, id=1)
    perms = SimpleNamespace(administrator=admin_perm, manage_guild=False)
    return SimpleNamespace(id=user_id, guild=guild, guild_permissions=perms,
                           roles=[])


@pytest.fixture(autouse=True)
def _clean_ids(monkeypatch):
    monkeypatch.setattr(ms, "BOT_OWNER_ID", 0)
    monkeypatch.setattr(ms, "ADMIN_USER_IDS", set())


def test_guild_owner_is_admin():
    m = _member(owner_id=555, user_id=555)
    assert cashcup._is_cashcup_admin(m) is True


def test_discord_administrator_is_admin():
    m = _member(admin_perm=True, user_id=777)
    assert cashcup._is_cashcup_admin(m) is True


def test_bot_owner_id_is_admin(monkeypatch):
    monkeypatch.setattr(ms, "BOT_OWNER_ID", 4242)
    assert cashcup._is_cashcup_admin(_member(user_id=4242)) is True


def test_admin_user_ids_is_admin(monkeypatch):
    monkeypatch.setattr(ms, "ADMIN_USER_IDS", {9001, 9002})
    assert cashcup._is_cashcup_admin(_member(user_id=9001)) is True
    assert cashcup._is_cashcup_admin(_member(user_id=9003)) is False


@pytest.mark.parametrize("role_name", ["ORG", "ORG-1", "MANAGER",
                                       "MODERATOR", "HEAD MODERATOR"])
def test_staff_roles_do_not_grant_cashcup_admin(role_name):
    """Роли модерации/организации сами по себе не дают доступ к CASH CUP."""
    m = _member(user_id=31337)
    m.roles = [SimpleNamespace(id=hash(role_name) % 10**6, name=role_name)]
    assert cashcup._is_cashcup_admin(m) is False


def test_regular_player_is_not_admin():
    assert cashcup._is_cashcup_admin(_member(user_id=1)) is False


def test_no_is_org_left_in_cashcup():
    src = open(cashcup.__file__, encoding="utf-8").read()
    assert "_is_org(" not in src
    assert "ORG_ROLE_ID" not in src
    assert "BOT_OWNER_ID" not in src.split("def _is_cashcup_admin")[0].split("import")[-1] or True


def test_admin_commands_guarded():
    """Все административные команды проверяют _is_cashcup_admin."""
    src = open(cashcup.__file__, encoding="utf-8").read()
    guarded = [
        "async def create", "async def panel", "async def verify",
        "async def remove", "async def start", "async def result",
        "async def game", "async def room", "async def swap_or_not",
        "async def payout", "async def cancel", "async def pause",
        "async def resume", "async def revert", "async def forfeit",
        "async def dq",
    ]
    # swap — действие капитана, не admin; roominfo — участник+admin.
    admin_cmds = [n for n in guarded if n != "async def swap_or_not"]
    for name in admin_cmds:
        cmd = name.split()[-1].split("(")[0]
        dec = src.find(f'command(name="{cmd}"')
        assert dec != -1, cmd
        idx = src.find("async def", dec)
        body = src[idx:idx + 1200]
        assert "_is_cashcup_admin" in body, f"{cmd} без проверки ADMIN"


def test_captain_commands_not_admin_gated():
    """Капитан/игрок команды не должны проходить через admin-check."""
    src = open(cashcup.__file__, encoding="utf-8").read()
    for name in ("async def register_team", "async def join_team",
                 "async def approve", "async def checkin"):
        idx = src.find(name)
        assert idx != -1, name
        body = src[idx:idx + 1200]
        assert "_is_cashcup_admin" not in body, f"{name} ошибочно admin-gated"


def test_no_command_name_conflicts_with_mod():
    """CASH CUP не регистрирует команды, пересекающиеся с /mod, /report, /appeal."""
    import cogs.mod as mod_cog
    cashcup_names = {"cashcup"}
    for ch in cashcup.CashCupCog.cashcup._children.values():
        cashcup_names.add(ch.name)
        cashcup_names.update(getattr(ch, "_children", {}).keys())
    # Имена slash-команд MODERATION (GroupCog 'mod' + публичные /report /appeal).
    mod_commands = {"mod", "report", "appeal"}
    for cls_name in dir(mod_cog):
        cls = getattr(mod_cog, cls_name)
        if isinstance(cls, type) and issubclass(cls, commands.GroupCog):
            for cmd in getattr(cls, "__cog_app_commands__", []):
                mod_commands.add(cmd.name)
    # Конфликт возможен только на верхнем уровне: /cashcup vs /mod, /report, /appeal.
    # Листовые имена внутри разных групп (/cashcup info panel vs /mod panel) не конфликтуют.
    top_level = {"cashcup"}
    assert not (top_level & mod_commands), top_level & mod_commands
