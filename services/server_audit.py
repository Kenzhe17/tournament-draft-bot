"""Read-only and allowlisted Discord server structure export.

This module intentionally does not import the bot token, database connection,
or the Discord client. It never reads message history, channel topics, user
profiles, invites, webhooks, or any secret configuration value.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

# IMPORTANT: These are exact, reviewed, non-secret config attribute names.
# Do not dynamically serialize config.__dict__ or os.environ.
CHANNEL_CONFIG_KEYS = (
    "WELCOME_CHANNEL_ID",
    "INFO_CHANNEL_ID",
    "NOTIFICATION_CHANNEL_ID",
    "ROOM_DATA_CHANNEL_ID",
    "ROOM_OPEN_NOTIFY_CHANNEL_ID",
    "TOURNAMENT_AUDIT_CHANNEL_ID",
    "MOD_LOG_CHANNEL_ID",
    "BR_ROOM_CHANNEL_ID",
    "BR_DATA_CHANNEL_ID",
    "BR_TEAM_CHANNEL_ID",
    "ULT_ROOM_CHANNEL_ID",
    "PET_HUNGER_CHANNEL_ID",
    "GAMES_CHANNEL_ID",
    "REP_LOG_CHANNEL_ID",
    # Legacy MM bindings are included only for migration planning.
    "MATCH_FOUND_CHANNEL_ID",
)
ROLE_CONFIG_KEYS = (
    "ORG_ROLE_ID",
    "MANAGER_ROLE_ID",
    "ORG_1_ROLE_ID",
    "ORG_2_ROLE_ID",
    "ORG_3_ROLE_ID",
    "NOTIFICATION_ROLE_ID",
    "BR_ROLE_ID",
    "ULT_ROLE_ID",
    "MUTE_ROLE_ID",
)
COLLECTION_CONFIG_KEYS = ("TOURNAMENT_CHANNELS", "MM_MATCH_CHANNEL_POOL")
MAX_REPORT_BYTES = 7 * 1024 * 1024


def audit_allowed(user_id: int, guild_id: int | None, *, owner_id: int, target_guild_id: int) -> bool:
    """Fail closed: configured bot owner on the configured home guild only."""
    return bool(
        owner_id > 0
        and target_guild_id > 0
        and guild_id == target_guild_id
        and user_id == owner_id
    )


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "name", value))


def _explicit_permissions(overwrite: Any) -> dict[str, bool]:
    """Only explicit True/False ACL flags; inherited (None) is omitted."""
    return {
        str(name): value
        for name, value in overwrite
        if value is not None and isinstance(value, bool)
    }


def _channel_record(channel: Any, role_ids: set[int]) -> dict[str, Any]:
    overwrites = []
    for target, overwrite in getattr(channel, "overwrites", {}).items():
        tid = _integer(getattr(target, "id", 0))
        if not tid:
            continue
        explicit = _explicit_permissions(overwrite)
        if not explicit:
            continue
        kind = "role" if tid in role_ids else "member"
        overwrites.append({
            "target_type": kind,
            "target_id": tid,
            "permissions": dict(sorted(explicit.items())),
        })
    overwrites.sort(key=lambda item: (item["target_type"], item["target_id"]))

    record: dict[str, Any] = {
        "id": _integer(channel.id),
        "name": str(channel.name),
        "type": _enum_name(getattr(channel, "type", None)),
        "position": _integer(getattr(channel, "position", 0)),
        "category_id": _integer(getattr(channel, "category_id", None)) or None,
        "permission_overwrites": overwrites,
    }

    # Only known-safe channel metadata: NO topic, description, pin/message,
    # invite, webhook, forum post, or room password content.
    for attr in ("nsfw", "slowmode_delay", "user_limit", "bitrate", "default_auto_archive_duration"):
        value = getattr(channel, attr, None)
        if type(value) in (bool, int):
            record[attr] = value
    synced = getattr(channel, "permissions_synced", None)
    if type(synced) is bool:
        record["permissions_synced"] = synced
    return record


def _role_record(role: Any) -> dict[str, Any]:
    perms = getattr(role, "permissions", ())
    enabled = sorted(str(name) for name, enabled in perms if enabled is True)
    color = getattr(role, "color", None)
    color_value = _integer(getattr(color, "value", color))
    return {
        "id": _integer(role.id),
        "name": str(role.name),
        "position": _integer(getattr(role, "position", 0)),
        "color": f"#{color_value & 0xFFFFFF:06X}",
        "hoist": bool(getattr(role, "hoist", False)),
        "mentionable": bool(getattr(role, "mentionable", False)),
        "managed": bool(getattr(role, "managed", False)),
        "permissions_value": str(_integer(getattr(perms, "value", 0))),
        "enabled_permissions": enabled,
    }


def _config_bindings(config: Any, channel_ids: set[int], role_ids: set[int]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Export ONLY named ID settings, never runtime secrets or raw env."""
    channel_bindings: dict[str, Any] = {}
    role_bindings: dict[str, Any] = {}
    warnings: list[str] = []

    for key in CHANNEL_CONFIG_KEYS:
        value = _integer(getattr(config, key, 0))
        channel_bindings[key] = {"id": value or None, "found": value in channel_ids if value else None}
        if value and value not in channel_ids:
            warnings.append(f"{key}: configured channel {value} was not returned by Discord")

    for key in ROLE_CONFIG_KEYS:
        value = _integer(getattr(config, key, 0))
        role_bindings[key] = {"id": value or None, "found": value in role_ids if value else None}
        if value and value not in role_ids:
            warnings.append(f"{key}: configured role {value} was not returned by Discord")

    for key in COLLECTION_CONFIG_KEYS:
        ids = [_integer(v) for v in (getattr(config, key, None) or [])]
        channel_bindings[key] = [
            {"id": cid, "found": cid in channel_ids}
            for cid in ids if cid > 0
        ]
        for cid in ids:
            if cid > 0 and cid not in channel_ids:
                warnings.append(f"{key}: configured channel {cid} was not returned by Discord")

    role_map = getattr(config, "ORG_ROLE_MAP", None) or {}
    channel_bindings["ORG_ROLE_MAP"] = [
        {
            "channel_id": _integer(cid),
            "channel_found": _integer(cid) in channel_ids,
            "role_id": _integer(rid),
            "role_found": _integer(rid) in role_ids,
        }
        for cid, rid in sorted(role_map.items(), key=lambda pair: _integer(pair[0]))
    ]
    return channel_bindings, role_bindings, warnings


def build_server_snapshot(guild: Any, channels: list[Any], roles: list[Any], config: Any) -> dict[str, Any]:
    """Produce a deterministic allowlisted JSON-safe report; no API writes."""
    channel_ids = {_integer(channel.id) for channel in channels}
    role_ids = {_integer(role.id) for role in roles}
    channel_records = [_channel_record(channel, role_ids) for channel in channels]
    channel_records.sort(key=lambda c: (c["position"], c["id"]))
    role_records = [_role_record(role) for role in roles]
    role_records.sort(key=lambda r: (-r["position"], r["id"]))
    category_records = [c for c in channel_records if c["type"] == "category"]
    channel_bindings, role_bindings, warnings = _config_bindings(config, channel_ids, role_ids)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "read_only",
        "guild": {
            "id": _integer(guild.id),
            "name": str(guild.name),
            "owner_id": _integer(getattr(guild, "owner_id", 0)) or None,
            "member_count_cached": _integer(getattr(guild, "member_count", 0)) or None,
            "features": sorted(str(item) for item in (getattr(guild, "features", None) or [])),
            "verification_level": _enum_name(getattr(guild, "verification_level", None)),
        },
        "summary": {
            "categories": len(category_records),
            "channels": len(channel_records),
            "roles": len(role_records),
            "channels_with_explicit_overwrites": sum(bool(c["permission_overwrites"]) for c in channel_records),
        },
        "categories": [{"id": c["id"], "name": c["name"], "position": c["position"]} for c in category_records],
        "channels": channel_records,
        "roles": role_records,
        "configured_channel_bindings": channel_bindings,
        "configured_role_bindings": role_bindings,
        "warnings": warnings,
        "limitations": [
            "All channels and roles are from Discord HTTP read endpoints at report time.",
            "Channel overrides are explicit ACLs, not a full effective-per-user permission evaluation.",
            "Environment-backed IDs may be overridden by runtime database settings.",
            "Message history, channel topics, invitations, webhooks, secrets, and user messages are intentionally excluded.",
            "Threads and their messages are not listed by Guild.fetch_channels().",
        ],
    }


def report_json_bytes(snapshot: dict[str, Any]) -> bytes:
    return (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
