"""RBAC Control Center.

Отдельная ролевая модель панели — НЕ переиспользует is_admin()/
is_org_check() бота, т.к. те пропускают организаторов.

Иерархия:
  owner       — BOT_OWNER_ID + CONTROL_CENTER_OWNER_IDS + владелец
                Discord-сервера → "*" (все разрешения)
  admin       — guild owner / Administrator / ADMIN_USER_IDS →
                все модульные разрешения, КРОМЕ rbac.manage и
                economy.finance (финансы — только по явному гранту)
  moderator   — явные гранты moderation.* и пр.
  organizer   — гранты tournament.* (без модерации/финансов)
  viewer      — только просмотр (panel.view, settings.view)

Поверх ролей действуют гранты admin_role_permissions (PG):
"perm" добавляет разрешение, "perm.*" — префиксное.
"*" в грантах = полный доступ.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PERM_PANEL_VIEW = "panel.view"
PERM_SETTINGS_VIEW = "settings.view"
PERM_SETTINGS_MANAGE = "settings.manage"
PERM_AUDIT_VIEW = "audit.view"
PERM_ACTIONS_EXECUTE = "actions.execute"
PERM_RBAC_MANAGE = "rbac.manage"
PERM_ECONOMY_FINANCE = "economy.finance"

DISCORD_ADMIN_PERMISSION = 0x8

# Базовые наборы по ролям. owner → "*" обрабатывается отдельно.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({
        PERM_PANEL_VIEW, PERM_SETTINGS_VIEW, PERM_SETTINGS_MANAGE,
        PERM_AUDIT_VIEW, PERM_ACTIONS_EXECUTE,
        "feature_flags.manage", "tournament.manage", "matchmaking.manage",
        "shop.manage", "players.manage", "ocr.manage", "games.manage",
        "discord.manage", "design.manage", "moderation.manage",
        "economy.manage", "vac.manage",
    }),
    "moderator": frozenset({
        PERM_PANEL_VIEW, PERM_SETTINGS_VIEW, PERM_AUDIT_VIEW,
        PERM_ACTIONS_EXECUTE,
        "moderation.manage", "vac.manage", "players.manage",
    }),
    "organizer": frozenset({
        PERM_PANEL_VIEW, PERM_SETTINGS_VIEW,
        PERM_ACTIONS_EXECUTE,
        "tournament.manage", "matchmaking.manage",
    }),
    "viewer": frozenset({
        PERM_PANEL_VIEW, PERM_SETTINGS_VIEW, PERM_AUDIT_VIEW,
    }),
}


def _perm_matches(grant: str, needed: str) -> bool:
    return (
        grant == "*" or grant == needed
        or (grant.endswith(".*") and needed.startswith(grant[:-1]))
    )


def has_permission(
    permissions: set[str], needed: str
) -> bool:
    """Проверка разрешения с поддержкой '*' и '<module>.*'."""
    if "*" in permissions:
        return True
    return any(_perm_matches(g, needed) for g in permissions)


def resolve_role(
    *,
    user_id: int,
    guild_owner: bool,
    guild_permissions: int,
    is_bot_admin_id: bool,
    is_panel_owner: bool,
) -> str | None:
    """Роль по Discord-атрибутам. None — доступа к панели нет.

    is_bot_admin_id — пользователь в ADMIN_USER_IDS/BOT_OWNER_ID бота
    (передаётся снаружи, чтобы не тянуть config бота в холодную).
    """
    if is_panel_owner:
        return "owner"
    if guild_owner or (guild_permissions & DISCORD_ADMIN_PERMISSION):
        return "admin"
    if is_bot_admin_id:
        return "admin"
    # Базовый доступ к панели — только по явным грантам
    # (moderator/organizer/viewer назначаются через admin_role_permissions
    # или отдельные Discord-роли — конфигурируется грантами).
    return None


def permissions_for_role(role: str | None) -> set[str]:
    if role == "owner":
        return {"*"}
    if role is None:
        return set()
    return set(ROLE_PERMISSIONS.get(role, ()))


async def effective_permissions(
    guild_id: int, user_id: int, role: str | None
) -> set[str]:
    """Permissions = role bundle + гранты из admin_role_permissions."""
    perms = permissions_for_role(role)
    if not perms:
        return perms
    try:
        from storage import admin_config_store as store
        grants = await store.user_permissions(guild_id, user_id)
        perms |= grants
    except Exception as exc:  # noqa: BLE001 — БД недоступна: только роль
        logger.warning("permission grants load failed: %s", exc)
    return perms
