"""Pure unit tests for owner gate and allowlisted server-audit report."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from services.server_audit import audit_allowed, build_server_snapshot, report_json_bytes


class FakeOverwrite:
    def __init__(self, **flags):
        self.flags = flags

    def __iter__(self):
        return iter(self.flags.items())


class FakePermissions:
    def __init__(self, **flags):
        self.flags = flags
        self.value = 123

    def __iter__(self):
        return iter(self.flags.items())


def fake_role(role_id=11, name="Organizer", position=5):
    return NS(
        id=role_id, name=name, position=position,
        color=NS(value=0x143B2D),
        hoist=False, mentionable=False, managed=False,
        permissions=FakePermissions(manage_channels=True, administrator=False),
    )


def fake_channel(cid, name, kind, category_id=None, overwrites=None, position=0):
    return NS(
        id=cid, name=name, type=NS(name=kind), category_id=category_id,
        position=position, overwrites=overwrites or {},
        nsfw=False, slowmode_delay=0, permissions_synced=True,
        topic="SECRET ROOM PASSWORD: 123456",  # NEVER exported
    )


def sample():
    # Hashable stand-ins for Discord role/member permission-overwrite targets.
    role = Target(11, "Organizer")
    member_override = Target(12345, "Private user")
    channels = [
        fake_channel(100, "TOURNAMENTS", "category", position=0),
        fake_channel(101, "tournament-1", "text", category_id=100,
                     overwrites={role: FakeOverwrite(view_channel=True, send_messages=False, connect=None),
                                 member_override: FakeOverwrite(view_channel=False)}, position=1),
    ]
    guild = NS(id=999, name="r!z3", owner_id=1, member_count=123,
               features=["COMMUNITY"], verification_level=NS(name="medium"))
    cfg = NS(
        GUILD_ID=999, BOT_OWNER_ID=1, TOURNAMENT_CHANNELS=[101],
        ORG_ROLE_MAP={101: 11}, ORG_ROLE_ID=11,
        ROOM_DATA_CHANNEL_ID=202, MM_MATCH_CHANNEL_POOL=[303],
        DISCORD_TOKEN="TOP_SECRET_TOKEN_ABC", MOD_LOG_WEBHOOK_URL="https://webhook/private",
    )
    return guild, channels, [fake_role()], cfg


class Target:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, Target) and self.id == other.id


def test_owner_gate_fails_closed():
    assert audit_allowed(1, 999, owner_id=1, target_guild_id=999)
    assert not audit_allowed(2, 999, owner_id=1, target_guild_id=999)
    assert not audit_allowed(1, 998, owner_id=1, target_guild_id=999)
    assert not audit_allowed(1, 999, owner_id=0, target_guild_id=999)
    assert not audit_allowed(1, 999, owner_id=1, target_guild_id=0)
    assert not audit_allowed(1, None, owner_id=1, target_guild_id=999)


def test_snapshot_serializable_and_secret_free():
    guild, channels, roles, cfg = sample()
    raw = report_json_bytes(build_server_snapshot(guild, channels, roles, cfg))
    data = json.loads(raw)
    assert data["schema_version"] == 1
    assert data["mode"] == "read_only"
    assert data["summary"] == {"categories": 1, "channels": 2, "roles": 1, "channels_with_explicit_overwrites": 1}
    assert "TOP_SECRET_TOKEN_ABC" not in raw.decode()
    assert "123456" not in raw.decode()
    assert "webhook/private" not in raw.decode()
    assert all("topic" not in ch for ch in data["channels"])
    assert "Private user" not in raw.decode()
    assert data["guild"]["id"] == 999
    assert data["configured_channel_bindings"]["TOURNAMENT_CHANNELS"] == [{"id": 101, "found": True}]
    assert data["configured_channel_bindings"]["ORG_ROLE_MAP"][0] == {
        "channel_id": 101, "channel_found": True, "role_id": 11, "role_found": True,
    }
    assert any("ROOM_DATA_CHANNEL_ID" in w for w in data["warnings"])
    assert data["roles"][0]["enabled_permissions"] == ["manage_channels"]
    assert data["channels"][1]["permission_overwrites"][0]["target_type"] == "member"
    assert data["channels"][1]["permission_overwrites"][1]["permissions"] == {"send_messages": False, "view_channel": True}


def test_no_raw_config_dump_and_zero_ids_are_safe():
    guild, channels, roles, cfg = sample()
    cfg.NOTIFICATION_CHANNEL_ID = 0
    data = build_server_snapshot(guild, channels, roles, cfg)
    assert data["configured_channel_bindings"]["NOTIFICATION_CHANNEL_ID"] == {"id": None, "found": None}
    assert "DISCORD_TOKEN" not in str(data)
    assert "MOD_LOG_WEBHOOK_URL" not in str(data)


def test_empty_discord_structure():
    guild, _, _, cfg = sample()
    data = build_server_snapshot(guild, [], [], cfg)
    assert data["summary"]["channels"] == 0
    assert data["summary"]["roles"] == 0
    assert any("TOURNAMENT_CHANNELS" in w for w in data["warnings"])
