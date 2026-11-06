"""Сервис организации Arbiter: org, settings, members, profiles, audit."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg
import discord

from arbiter.db import init_arbiter_schema
from arbiter.enums import OrgRole
from arbiter.models import (
    dumps,
    loads,
    new_id,
    Organization,
    OrgMember,
    OrgSettings,
    UserProfile,
    utcnow,
)
from storage.db import get_pool

logger = logging.getLogger(__name__)

_STAFF_ROLES = {OrgRole.OWNER, OrgRole.ADMIN, OrgRole.MANAGER, OrgRole.HEAD_REF}
_REF_ROLES = {OrgRole.HEAD_REF, OrgRole.REFEREE}


class OrgError(Exception):
    pass


def _row_to_org(row: asyncpg.Record) -> Organization:
    return Organization(
        id=row["id"], discord_guild_id=row["discord_guild_id"],
        name=row["name"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _row_to_settings(row: asyncpg.Record) -> OrgSettings:
    return OrgSettings(
        id=row["id"], organization_id=row["organization_id"],
        admin_role_id=row["admin_role_id"], referee_role_id=row["referee_role_id"],
        match_category_id=row["match_category_id"],
        match_log_channel_id=row["match_log_channel_id"],
        evidence_channel_id=row["evidence_channel_id"],
        web_permissions=loads(row["web_permissions"]),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _row_to_profile(row: asyncpg.Record) -> UserProfile:
    return UserProfile(
        id=row["id"], discord_user_id=row["discord_user_id"],
        display_name=row["display_name"], country_code=row["country_code"],
        bio=row["bio"], profile_visibility=row["profile_visibility"],
        open_to_work=row["open_to_work"], avatar_url=row["avatar_url"],
        contact_email=row["contact_email"],
        show_contact_email=row["show_contact_email"],
        social_links=loads(row["social_links"]),
        game_experiences=loads(row["game_experiences"], []),
        field_roles=loads(row["field_roles"], []),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _row_to_member(row: asyncpg.Record) -> OrgMember:
    return OrgMember(
        id=row["id"], organization_id=row["organization_id"],
        user_profile_id=row["user_profile_id"],
        role=OrgRole(row["role"]),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class OrgService:
    """Реестр организаций и членства (одна org = один guild)."""

    async def get_or_create_org(self, guild: discord.Guild, creator: discord.Member) -> Organization:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_organizations WHERE discord_guild_id = $1",
                str(guild.id),
            )
            if row:
                return _row_to_org(row)

            org_id = new_id()
            now = utcnow()
            await conn.execute(
                "INSERT INTO arb_organizations (id, discord_guild_id, name, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                org_id, str(guild.id), guild.name, now, now,
            )
            await conn.execute(
                "INSERT INTO arb_org_settings (id, organization_id) VALUES ($1, $2)",
                new_id(), org_id,
            )
            profile = await self._get_or_create_profile(conn, creator)
            await conn.execute(
                "INSERT INTO arb_org_members (id, organization_id, user_profile_id, role) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
                new_id(), org_id, profile.id, OrgRole.OWNER.value,
            )
            return Organization(id=org_id, discord_guild_id=str(guild.id), name=guild.name)

    async def get_org_by_guild(self, guild_id: int | str) -> Organization | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_organizations WHERE discord_guild_id = $1",
                str(guild_id),
            )
            return _row_to_org(row) if row else None

    async def get_org_by_id(self, org_id: str) -> Organization | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM arb_organizations WHERE id = $1", org_id)
            return _row_to_org(row) if row else None

    async def get_settings(self, org_id: str) -> OrgSettings | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_org_settings WHERE organization_id = $1", org_id
            )
            return _row_to_settings(row) if row else None

    async def update_settings(self, org_id: str, **fields: Any) -> OrgSettings:
        allowed = {"admin_role_id", "referee_role_id", "match_category_id",
                   "match_log_channel_id", "evidence_channel_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            settings = await self.get_settings(org_id)
            if not settings:
                raise OrgError("Настройки организации не найдены")
            return settings
        pool = await get_pool()
        sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE arb_org_settings SET {sets}, updated_at = $1 "
                f"WHERE organization_id = ${len(updates)+2} RETURNING *",
                utcnow(), *updates.values(), org_id,
            )
            if not row:
                raise OrgError("Настройки организации не найдены")
            return _row_to_settings(row)

    # ---------- profiles ----------

    async def _get_or_create_profile(self, conn: asyncpg.Connection, user: discord.abc.User) -> UserProfile:
        row = await conn.fetchrow(
            "SELECT * FROM arb_user_profiles WHERE discord_user_id = $1", str(user.id)
        )
        if row:
            return _row_to_profile(row)
        profile = UserProfile(
            discord_user_id=str(user.id),
            display_name=user.display_name,
            avatar_url=str(user.display_avatar.url) if user.display_avatar else None,
        )
        now = utcnow()
        await conn.execute(
            "INSERT INTO arb_user_profiles (id, discord_user_id, display_name, avatar_url, "
            "created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6)",
            profile.id, profile.discord_user_id, profile.display_name,
            profile.avatar_url, now, now,
        )
        return profile

    async def get_or_create_profile(self, user: discord.abc.User) -> UserProfile:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await self._get_or_create_profile(conn, user)

    async def get_profile_by_id(self, profile_id: str) -> UserProfile | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_user_profiles WHERE id = $1", profile_id
            )
            return _row_to_profile(row) if row else None

    async def get_profile_by_discord_id(self, discord_user_id: str | int) -> UserProfile | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_user_profiles WHERE discord_user_id = $1", str(discord_user_id)
            )
            return _row_to_profile(row) if row else None

    # ---------- members ----------

    async def get_member(self, org_id: str, user_profile_id: str) -> OrgMember | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_org_members WHERE organization_id = $1 AND user_profile_id = $2",
                org_id, user_profile_id,
            )
            return _row_to_member(row) if row else None

    async def get_member_role_for_user(self, org_id: str, discord_user_id: str | int) -> OrgRole | None:
        profile = await self.get_profile_by_discord_id(discord_user_id)
        if not profile:
            return None
        member = await self.get_member(org_id, profile.id)
        return member.role if member else None

    async def list_members(self, org_id: str) -> list[tuple[OrgMember, UserProfile]]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.*, p.discord_user_id AS p_discord_user_id, p.display_name AS p_display_name "
                "FROM arb_org_members m JOIN arb_user_profiles p ON p.id = m.user_profile_id "
                "WHERE m.organization_id = $1 ORDER BY m.created_at",
                org_id,
            )
            result = []
            for r in rows:
                member = _row_to_member(r)
                profile = UserProfile(
                    id=member.user_profile_id, discord_user_id=r["p_discord_user_id"],
                    display_name=r["p_display_name"],
                )
                result.append((member, profile))
            return result

    async def add_member(self, org_id: str, discord_user_id: str | int, role: OrgRole,
                         added_by: str | None = None) -> OrgMember:
        profile = await self.get_or_create_profile_by_id(discord_user_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM arb_org_members WHERE organization_id = $1 AND user_profile_id = $2",
                org_id, profile.id,
            )
            if existing:
                await conn.execute(
                    "UPDATE arb_org_members SET role = $1, updated_at = $2 WHERE id = $3",
                    role.value, utcnow(), existing["id"],
                )
                return _row_to_member({**dict(existing), "role": role.value})
            member = OrgMember(organization_id=org_id, user_profile_id=profile.id, role=role)
            now = utcnow()
            await conn.execute(
                "INSERT INTO arb_org_members (id, organization_id, user_profile_id, role, created_at, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                member.id, org_id, profile.id, role.value, now, now,
            )
            await self._audit(conn, org_id, added_by, "member.add",
                              "org_member", member.id,
                              {"discord_user_id": str(discord_user_id), "role": role.value})
            return member

    async def remove_member(self, org_id: str, discord_user_id: str | int,
                            actor_profile_id: str | None = None) -> bool:
        profile = await self.get_profile_by_discord_id(discord_user_id)
        if not profile:
            return False
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM arb_org_members WHERE organization_id = $1 AND user_profile_id = $2 RETURNING id",
                org_id, profile.id,
            )
            if row:
                await self._audit(conn, org_id, actor_profile_id, "member.remove",
                                  "org_member", row["id"],
                                  {"discord_user_id": str(discord_user_id)})
                return True
            return False

    async def get_or_create_profile_by_id(self, discord_user_id: str | int) -> UserProfile:
        """Профиль по discord id; при отсутствии создаёт минимальный."""
        existing = await self.get_profile_by_discord_id(discord_user_id)
        if existing:
            return existing
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_user_profiles WHERE discord_user_id = $1", str(discord_user_id)
            )
            if row:
                return _row_to_profile(row)
            profile = UserProfile(discord_user_id=str(discord_user_id))
            now = utcnow()
            await conn.execute(
                "INSERT INTO arb_user_profiles (id, discord_user_id, created_at, updated_at) "
                "VALUES ($1,$2,$3,$4) ON CONFLICT (discord_user_id) DO NOTHING",
                profile.id, profile.discord_user_id, now, now,
            )
            row = await conn.fetchrow(
                "SELECT * FROM arb_user_profiles WHERE discord_user_id = $1", str(discord_user_id)
            )
            return _row_to_profile(row)

    # ---------- permissions ----------

    async def require_role(self, interaction: discord.Interaction, *roles: OrgRole) -> OrgMember:
        """Проверка членства; бросает OrgError если нет прав."""
        org = await self.get_org_by_guild(interaction.guild_id)
        if not org:
            raise OrgError("Организация не настроена на этом сервере. Используйте /arb setup.")
        profile = await self.get_or_create_profile(interaction.user)
        member = await self.get_member(org.id, profile.id)
        if not member or member.role not in roles:
            raise OrgError("Недостаточно прав.")
        return member

    async def is_staff(self, org_id: str, discord_user_id: str | int) -> bool:
        role = await self.get_member_role_for_user(org_id, discord_user_id)
        return role in _STAFF_ROLES if role else False

    async def is_referee(self, org_id: str, discord_user_id: str | int) -> bool:
        role = await self.get_member_role_for_user(org_id, discord_user_id)
        return role in _REF_ROLES if role else False

    # ---------- audit ----------

    @staticmethod
    async def _audit(conn: asyncpg.Connection, org_id: str, actor_id: str | None,
                     action: str, target_type: str | None = None,
                     target_id: str | None = None, metadata: dict | None = None) -> None:
        await conn.execute(
            "INSERT INTO arb_audit_log (id, organization_id, actor_id, action, target_type, target_id, metadata) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            new_id(), org_id, actor_id, action, target_type, target_id,
            dumps(metadata) if metadata else None,
        )

    async def audit(self, org_id: str, actor_id: str | None, action: str,
                    target_type: str | None = None, target_id: str | None = None,
                    metadata: dict | None = None) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await self._audit(conn, org_id, actor_id, action, target_type, target_id, metadata)


org_service = OrgService()
