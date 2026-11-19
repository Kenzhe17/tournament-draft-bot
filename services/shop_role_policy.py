"""Validate Discord roles at assignment time; catalog entries are not authority."""
from __future__ import annotations

import logging

import config

logger = logging.getLogger(__name__)

_DANGEROUS = (
    'administrator', 'manage_guild', 'manage_roles', 'manage_channels',
    'manage_webhooks', 'manage_messages', 'manage_threads', 'moderate_members',
    'kick_members', 'ban_members', 'mute_members', 'deafen_members', 'move_members',
    'mention_everyone', 'view_audit_log',
)


async def can_sell_role(guild, role) -> bool:
    if role is None or role.id == guild.id or getattr(role, 'managed', False):
        return False
    if getattr(role, 'guild', guild).id != guild.id:
        return False
    permissions = getattr(role, 'permissions', None)
    if permissions is None or any(getattr(permissions, key, False) for key in _DANGEROUS):
        return False
    protected = {getattr(config, key, 0) for key in (
        'ORG_ROLE_ID', 'ORG_1_ROLE_ID', 'ORG_2_ROLE_ID', 'ORG_3_ROLE_ID',
        'MANAGER_ROLE_ID', 'MUTE_ROLE_ID',
    )}
    protected.update(config.ORG_ROLE_MAP.values())
    if role.id in protected:
        return False
    # Staff roles are dynamic and must be checked against current storage.
    if config.DATABASE_URL:
        try:
            from storage.moderation_store import moderation_store
            settings = await moderation_store.settings(guild.id)
            if role.id in {settings.get('moderator_role_id'), settings.get('head_role_id')}:
                return False
        except Exception:
            logger.warning("Shop role validation: staff settings unavailable", exc_info=True)
            return False
    # Permissionless roles can still unlock private staff channels via ACLs.
    # Color roles and public club access roles do not need those overwrites.
    for channel in guild.channels:
        overwrite = channel.overwrites_for(role)
        if getattr(overwrite, 'view_channel', None) is True:
            baseline = channel.permissions_for(guild.default_role)
            if not baseline.view_channel:
                return False
        if any(getattr(overwrite, key, None) is True for key in _DANGEROUS):
            return False
    return True
