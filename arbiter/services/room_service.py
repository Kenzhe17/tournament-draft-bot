"""RoomService — комнаты матчей (порт team rooms / match rooms Arbiter).

Создаёт category + text/voice каналы под матч, выдаёт права командам,
архивирует историю и удаляет при закрытии.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
import asyncpg

from arbiter.models import new_id, utcnow
from arbiter.services.match_service import match_service
from arbiter.services.org_service import org_service
from storage.db import get_pool

logger = logging.getLogger(__name__)


class RoomError(Exception):
    pass


class RoomService:
    async def _get_or_create_room_row(self, conn, match_id: str) -> asyncpg.Record:
        row = await conn.fetchrow(
            "SELECT * FROM arb_match_rooms WHERE match_id=$1", match_id)
        if row:
            return row
        row = await conn.fetchrow(
            "INSERT INTO arb_match_rooms (id, match_id, created_at, updated_at) "
            "VALUES ($1,$2,$3,$4) RETURNING *", new_id(), match_id, utcnow(), utcnow())
        return row

    async def create_match_room(self, guild: discord.Guild, match_id: str,
                                *, member_ids_a: list[int] | None = None,
                                member_ids_b: list[int] | None = None,
                                role_a_id: int | None = None,
                                role_b_id: int | None = None) -> dict:
        """Создать комнату матча: category + общий text/voice + team text/voice."""
        match = await match_service.get_match(match_id)
        if not match:
            raise RoomError("Матч не найден")
        org = await org_service.get_org_by_id(match.organization_id)
        if not org:
            raise RoomError("Организация не найдена")
        settings = await org_service.get_settings(org.id)
        parent = None
        if settings and settings.match_category_id:
            parent = guild.get_channel(int(settings.match_category_id))

        me = guild.me
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            me: discord.PermissionOverwrite(manage_channels=True),
        }
        # судьи/админы видят всё
        members = await org_service.list_members(org.id)
        for m, p in members:
            if m.role.value in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF", "REFEREE"):
                try:
                    member = guild.get_member(int(p.discord_user_id))
                    if member:
                        overwrites[member] = discord.PermissionOverwrite(
                            view_channel=True, send_messages=True,
                            connect=True, speak=True)
                except (ValueError, discord.HTTPException):
                    pass

        safe_name = f"m-{match.public_code}".lower().replace(" ", "-")[:90]
        category = await guild.create_category(f"🏟️ {match.team_a_name} vs {match.team_b_name}"[:95],
                                               overwrites=overwrites, parent=parent)

        async def _text_room(name: str, extra: dict | None = None) -> discord.TextChannel:
            ow = dict(overwrites)
            if extra:
                ow.update(extra)
            return await guild.create_text_channel(
                name, category=category, overwrites=ow)

        async def _voice_room(name: str, extra: dict | None = None) -> discord.VoiceChannel:
            ow = dict(overwrites)
            if extra:
                ow.update(extra)
            return await guild.create_voice_channel(
                name, category=category, overwrites=ow)

        result: dict[str, Any] = {"category_id": str(category.id)}

        # Комната A
        ow_a = {}
        if member_ids_a:
            for uid in member_ids_a:
                try:
                    mm = guild.get_member(uid)
                    if mm:
                        ow_a[mm] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True)
                except discord.HTTPException:
                    pass
        if role_a_id:
            r = guild.get_role(role_a_id)
            if r:
                ow_a[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True)
        ta_text = await _text_room(f"{safe_name}-a", ow_a)
        ta_voice = await _voice_room(f"{safe_name}-a-v", ow_a)
        result.update(team_a_text=str(ta_text.id), team_a_voice=str(ta_voice.id))

        # Комната B
        ow_b = {}
        if member_ids_b:
            for uid in member_ids_b:
                try:
                    mm = guild.get_member(uid)
                    if mm:
                        ow_b[mm] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True)
                except discord.HTTPException:
                    pass
        if role_b_id:
            r = guild.get_role(role_b_id)
            if r:
                ow_b[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True)
        tb_text = await _text_room(f"{safe_name}-b", ow_b)
        tb_voice = await _voice_room(f"{safe_name}-b-v", ow_b)
        result.update(team_b_text=str(tb_text.id), team_b_voice=str(tb_voice.id))

        # Общая комната (судьи + оба состава)
        ow_all = {}
        for uid in (member_ids_a or []) + (member_ids_b or []):
            try:
                mm = guild.get_member(uid)
                if mm:
                    ow_all[mm] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, connect=True, speak=True)
            except discord.HTTPException:
                pass
        main_text = await _text_room(f"{safe_name}-main", ow_all)
        main_voice = await _voice_room(f"{safe_name}-main-v", ow_all)
        result.update(main_text=str(main_text.id), main_voice=str(main_voice.id))

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE arb_match_rooms SET category_id=$1, team_a_text_channel_id=$2, "
                "team_a_voice_channel_id=$3, team_b_text_channel_id=$4, "
                "team_b_voice_channel_id=$5, text_channel_id=$6, voice_channel_id=$7, "
                "updated_at=$8 WHERE match_id=$9",
                result["category_id"], result["team_a_text"], result["team_a_voice"],
                result["team_b_text"], result["team_b_voice"],
                result["main_text"], result["main_voice"], utcnow(), match_id)
        return result

    async def get_room(self, match_id: str) -> dict | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_match_rooms WHERE match_id=$1", match_id)
            return dict(row) if row else None

    async def archive_and_delete(self, guild: discord.Guild, match_id: str,
                                 log_channel_id: int | None = None) -> None:
        """Архивировать историю комнат в лог-канал и удалить каналы."""
        room = await self.get_room(match_id)
        if not room:
            return
        channel_ids = [
            room.get("text_channel_id"), room.get("voice_channel_id"),
            room.get("team_a_text_channel_id"), room.get("team_a_voice_channel_id"),
            room.get("team_b_text_channel_id"), room.get("team_b_voice_channel_id"),
        ]
        category = guild.get_channel(int(room["category_id"])) if room.get("category_id") else None
        if category:
            for ch in list(category.channels):
                if str(ch.id) not in [str(c) for c in channel_ids if c]:
                    channel_ids.append(str(ch.id))

        # архив: последние сообщения text-каналов -> лог
        if log_channel_id:
            log_ch = guild.get_channel(log_channel_id)
            if log_ch:
                for cid in channel_ids:
                    ch = guild.get_channel(int(cid)) if cid else None
                    if isinstance(ch, discord.TextChannel):
                        try:
                            async for msg in ch.history(limit=20):
                                if msg.author.bot:
                                    continue
                                embed = discord.Embed(
                                    description=msg.content[:1500] or "(вложение)",
                                    color=0x414146,
                                    timestamp=msg.created_at,
                                )
                                embed.set_author(name=msg.author.display_name,
                                                 icon_url=msg.author.display_avatar.url)
                                embed.set_footer(text=f"#{ch.name} • архив")
                                await log_ch.send(embed=embed)
                        except discord.HTTPException as e:
                            logger.warning("Archive %s failed: %s", ch.name, e)

        # удаление
        for cid in channel_ids:
            ch = guild.get_channel(int(cid)) if cid else None
            if ch:
                try:
                    await ch.delete(reason="Match room archived")
                except discord.HTTPException as e:
                    logger.warning("Delete channel %s failed: %s", cid, e)
        if category:
            try:
                await category.delete(reason="Match room archived")
            except discord.HTTPException:
                pass
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM arb_match_rooms WHERE match_id=$1", match_id)


room_service = RoomService()
