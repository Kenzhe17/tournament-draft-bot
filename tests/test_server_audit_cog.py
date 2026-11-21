"""Integration checks: cog loads, /admin server_audit binds, owner gate strict."""

from __future__ import annotations

import asyncio
import inspect

import cogs.server_audit as server_audit_cog
from cogs.groups import ROOT_GROUPS, admin_group
from services.server_audit import audit_allowed


class _FakeBot:
    pass


def test_server_audit_registered_under_admin_group():
    cog = server_audit_cog.ServerAuditCog(_FakeBot())
    asyncio.run(cog.cog_load())

    assert admin_group in ROOT_GROUPS
    cmd = admin_group.get_command("server_audit")
    assert cmd is not None
    assert cmd.binding is cog


def test_command_source_enforces_owner_and_ephemeral():
    cmd = admin_group.get_command("server_audit")
    src = inspect.getsource(cmd.callback)
    assert "audit_allowed" in src
    assert "BOT_OWNER_ID" in src
    assert "GUILD_ID" in src
    assert "ephemeral=True" in src
    assert "guild_only" in src


def test_audit_gate_rejects_admin_organizer_other_guild():
    # user 2 is a guild admin but not the bot owner -> denied
    assert not audit_allowed(2, 999, owner_id=1, target_guild_id=999)
    # owner on wrong guild -> denied
    assert not audit_allowed(1, 1000, owner_id=1, target_guild_id=999)
    # owner on right guild -> allowed
    assert audit_allowed(1, 999, owner_id=1, target_guild_id=999)
