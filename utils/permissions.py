"""Проверки прав доступа."""

import discord
from discord import app_commands

from config import ADMIN_USER_IDS, BOT_OWNER_ID, ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID
from storage.organizer_store import organizer_store


def is_admin() -> app_commands.check:
    """Декоратор: только администратор сервера, роль 'org', ORG-1/2/3 или создатель бота."""

    async def predicate(interaction: discord.Interaction) -> bool:
        # Проверяем права админа ИЛИ ваш конкретный Discord ID
        if interaction.user.guild_permissions.administrator or interaction.user.id == BOT_OWNER_ID or interaction.user.id in ADMIN_USER_IDS:
            return True

        # Check for 'org' role
        if interaction.guild:
            org_role = discord.utils.get(interaction.guild.roles, name="org")
            if org_role and org_role in interaction.user.roles:
                return True

            # Check for ORG-1, ORG-2, ORG-3 roles
            org_role_ids = [ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID]
            for role_id in org_role_ids:
                role = interaction.guild.get_role(role_id)
                if role and role in interaction.user.roles:
                    return True

        raise app_commands.CheckFailure(
            "Эта команда доступна только администраторам или организаторам (роль 'org', ORG-1/2/3)."
        )

    return app_commands.check(predicate)


def is_org() -> app_commands.check:
    """Декоратор: только роль 'org', ORG-1/2/3 или администратор сервера или создатель бота."""

    async def predicate(interaction: discord.Interaction) -> bool:
        # Проверяем права админа ИЛИ ваш конкретный Discord ID
        if interaction.user.guild_permissions.administrator or interaction.user.id == BOT_OWNER_ID or interaction.user.id in ADMIN_USER_IDS:
            return True

        # Check for 'org' role
        if interaction.guild:
            org_role = discord.utils.get(interaction.guild.roles, name="org")
            if org_role and org_role in interaction.user.roles:
                return True

            # Check for ORG-1, ORG-2, ORG-3 roles
            org_role_ids = [ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID]
            for role_id in org_role_ids:
                role = interaction.guild.get_role(role_id)
                if role and role in interaction.user.roles:
                    return True

        raise app_commands.CheckFailure(
            "Эта команда доступна только организаторам (роль 'org', ORG-1/2/3) или администраторам."
        )

    return app_commands.check(predicate)


def is_admin_check(user: discord.Member, guild: discord.Guild) -> bool:
    """Проверка: является ли пользователь администратором или имеет роль 'org', ORG-1/2/3."""
    if user.guild_permissions.administrator or user.id == BOT_OWNER_ID or user.id in ADMIN_USER_IDS:
        return True

    # Check for 'org' role
    org_role = discord.utils.get(guild.roles, name="org")
    if org_role and org_role in user.roles:
        return True

    # Check for ORG-1, ORG-2, ORG-3 roles
    org_role_ids = [ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID]
    for role_id in org_role_ids:
        role = guild.get_role(role_id)
        if role and role in user.roles:
            return True

    return False


def is_org_check(user: discord.Member, guild: discord.Guild) -> bool:
    """Проверка: является ли пользователь организатором (роль 'org', ORG-1/2/3) или администратором."""
    if user.guild_permissions.administrator or user.id == BOT_OWNER_ID or user.id in ADMIN_USER_IDS:
        return True

    # Check for 'org' role
    org_role = discord.utils.get(guild.roles, name="org")
    if org_role and org_role in user.roles:
        return True

    # Check for ORG-1, ORG-2, ORG-3 roles
    org_role_ids = [ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID]
    for role_id in org_role_ids:
        role = guild.get_role(role_id)
        if role and role in user.roles:
            return True

    return False


def can_manage_tournament(user, guild: discord.Guild, tournament) -> bool:
    """Единая проверка прав управления КОНКРЕТНЫМ турниром.

    Разрешено:
    - владельцу бота, администраторам сервера, ADMIN_USER_IDS;
    - глобальной роли 'org' и ролям ORG-1/2/3 (is_org_check);
    - создателю турнира (tournament.organizer_id);
    - основному и дополнительным организаторам канала этого
      турнира (organizer_store), при этом organizer_store.tournament_id
      обязан совпадать с tournament.id — назначения от предыдущего
      турнира доступа не дают (пустой tournament_id — legacy-запись
      без привязки, считается действующей для текущего турнира
      канала);
    - пользователю с ролью из ORG_ROLE_MAP, назначенной именно
      для канала этого турнира.

    Организатор чужого канала/турнира → False. Капитан команды без
    организаторских прав → False.
    """
    if user is None or tournament is None or guild is None:
        return False
    try:
        if is_org_check(user, guild):
            return True
    except Exception:
        pass
    uid = getattr(user, "id", 0)
    if not uid:
        return False
    # Создатель турнира
    if getattr(tournament, "organizer_id", 0) == uid:
        return True
    t_gid = getattr(tournament, "guild_id", 0) or getattr(guild, "id", 0)
    t_cid = getattr(tournament, "channel_id", 0)
    t_tid = str(getattr(tournament, "id", "") or "")
    # Основной + дополнительные организаторы канала (с привязкой
    # к этому турниру)
    try:
        from storage.organizer_store import organizer_store

        orgs = organizer_store.get(t_gid, t_cid)
        if orgs and orgs.is_organizer(uid):
            bound_tid = str(getattr(orgs, "tournament_id", "") or "")
            if not bound_tid or bound_tid == t_tid:
                return True
    except Exception:
        pass
    # ORG_ROLE_MAP: роль, назначенная для канала этого турнира
    try:
        from config import ORG_ROLE_MAP

        role_id = ORG_ROLE_MAP.get(int(t_cid))
        if role_id:
            role = guild.get_role(int(role_id))
            if role is not None and role in getattr(user, "roles", []):
                return True
    except Exception:
        pass
    return False


def can_manage_channel_tournament(user, guild: discord.Guild, channel_id: int) -> bool:
    """Проверка прав на турнир в канале (если турнир существует).

    Нет турнира → fallback на is_channel_organizer (управление
    каналом организаторов). Есть турнир → can_manage_tournament
    с проверкой привязки tournament_id.
    """
    try:
        from storage.json_store import store

        tournament = store.get(guild.id, channel_id)
    except Exception:
        tournament = None
    if tournament is None:
        return is_channel_organizer(user, guild, channel_id)
    return can_manage_tournament(user, guild, tournament)


# Обратная совместимость: старое имя той же проверки.
can_manage_tournament_results = can_manage_tournament


def is_channel_organizer(user: discord.Member, guild: discord.Guild, channel_id: int) -> bool:
    """Проверка: является ли пользователь организатором канала."""
    # Check if user is global org/admin
    if is_org_check(user, guild):
        return True

    # Check channel organizers
    channel_orgs = organizer_store.get(guild.id, channel_id)
    if channel_orgs and channel_orgs.is_organizer(user.id):
        return True

    return False


def is_channel_organizer_decorator() -> app_commands.check:
    """Декоратор: только организатор канала или глобальный org/admin."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise app_commands.CheckFailure("Эта команда доступна только на сервере.")

        if is_channel_organizer(interaction.user, interaction.guild, interaction.channel_id):
            return True

        raise app_commands.CheckFailure(
            "Эта команда доступна только организаторам канала или глобальным организаторам."
        )

    return app_commands.check(predicate)


def is_bot_owner() -> app_commands.check:
    """Декоратор: только владелец бота."""

    async def predicate(interaction: discord.Interaction) -> bool:
        # Проверяем конкретный Discord ID владельца бота из конфига
        if interaction.user.id == BOT_OWNER_ID:
            return True

        raise app_commands.CheckFailure(
            "Эта команда доступна только владельцу бота."
        )

    return app_commands.check(predicate)
