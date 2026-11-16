"""One durable announcement per tournament/format/action, without broad mentions."""
from __future__ import annotations

import asyncio
import json
import logging

import discord

import config
from services.discord_cfg import channel_id
from services.notification_preferences import role_id
from storage.json_atomic import atomic_write_json
from storage.server_layout_store import server_layout_store
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)
STATE_PATH = config.DATA_DIR / 'tournament_notifications.json'
_LOCKS: dict[int, asyncio.Lock] = {}


async def announce(guild, format_key: str, tournament_id: str, source_channel_id: int,
                   message_id: int, *, is_test: bool = False) -> str:
    if is_test or not config.guild_allowed(guild.id):
        return 'skipped'
    async with _LOCKS.setdefault(guild.id, asyncio.Lock()):
        data = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        key = f'{guild.id}:{format_key}:{tournament_id}:created'
        if key in data:
            return data[key]['status']
        cid = server_layout_store.binding(guild.id, 'announcements') or channel_id(
            guild.id, 'discord.notification_channel_id', config.NOTIFICATION_CHANNEL_ID)
        channel = guild.get_channel(cid)
        if channel is None or channel.guild.id != guild.id:
            return 'unconfigured'
        source = guild.get_channel(source_channel_id)
        # Never publish a link/notification for an event hidden from this audience.
        if source is None or not source.permissions_for(guild.default_role).view_channel:
            return 'private'
        rid = role_id(guild.id, format_key)
        if not rid and format_key == 'cs':
            rid = channel_id(guild.id, 'discord.notification_role_id', config.NOTIFICATION_ROLE_ID)
        role = guild.get_role(rid) if rid else None
        mention = role.mention if role else ''
        labels = {'cs': 'CS 4×4', 'br': 'Battle Royale', 'ult': 'Ultimatum'}
        view = DesignView(title=f"Новый турнир · {labels[format_key]}", timeout=None)
        try:
            from services.cv2_templates import render_template
            from storage import admin_config_store
            template = await admin_config_store.get_template(guild.id, 'tournament.announcement') \
                or await admin_config_store.get_template(0, 'tournament.announcement')
            if template:
                custom = render_template(template['payload'])
                if custom.total_children_count <= 32:
                    view = custom
        except Exception:
            logger.warning('Tournament announcement template unavailable; using default', exc_info=True)
        url = f'https://discord.com/channels/{guild.id}/{source_channel_id}'
        if message_id:
            url += f'/{message_id}'
        view.text(f'{mention}\n[Открыть турнир]({url})'.strip())
        data[key] = {'status': 'pending', 'channel_id': channel.id}
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(STATE_PATH, data)
        # Pending remains on timeouts. An operator reconciles it instead of sending a second ping.
        message = await channel.send(view=view, allowed_mentions=discord.AllowedMentions(
            users=False, roles=[role] if role else False, everyone=False, replied_user=False))
        data[key].update(status='sent', message_id=message.id)
        atomic_write_json(STATE_PATH, data)
        return 'sent'


async def announce_safe(*args, **kwargs):
    try:
        return await announce(*args, **kwargs)
    except Exception:
        logger.exception('Tournament created, but announcement not confirmed; will not retry blindly')
        return 'uncertain'
