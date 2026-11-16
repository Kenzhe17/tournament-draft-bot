"""Read-only tournament discovery for the club Hub; no credentials in responses."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config import DATABASE_URL
from storage.br_store import br_store
from storage.json_store import store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    format: str
    channel_id: int
    message_id: int
    state: str
    organizer_id: int
    players: int | None = None


def visible_to(member, guild, channel_id: int) -> bool:
    channel = guild.get_channel(channel_id)
    return bool(channel and channel.permissions_for(member).view_channel)


async def active_events(guild, member) -> tuple[list[Event], bool]:
    """Filter guild and channel permissions BEFORE disclosing an event."""
    events = []
    for tournament in store.get_by_guild(guild.id):
        if getattr(tournament, 'mm_search_channel_id', 0) or tournament.is_test:
            continue
        phase = getattr(tournament.phase, 'value', tournament.phase)
        if phase == 'complete' or not visible_to(member, guild, tournament.channel_id):
            continue
        events.append(Event('CS 4×4', tournament.channel_id,
                            tournament.message_id or 0,
                            ('Регистрация открыта' if tournament.registration.value == 'open' else 'Регистрация закрыта') if phase == 'setup' else 'Идёт турнир',
                            tournament.organizer_id, len(tournament.all_players | set(tournament.players_pool))))
    for tournament in br_store.get_by_guild(guild.id):
        phase = getattr(tournament.phase, 'value', tournament.phase)
        if phase == 'complete' or not visible_to(member, guild, tournament.channel_id):
            continue
        events.append(Event('Battle Royale', tournament.channel_id,
                            tournament.message_id or 0,
                            'Регистрация' if phase == 'registration' else 'Идёт турнир',
                            tournament.organizer_id, len(tournament.players)))
    unavailable = not bool(DATABASE_URL)
    if DATABASE_URL:
        try:
            from ultimatum.store import ultimatum_store
            for tournament in await ultimatum_store.list_active_all():
                if tournament.guild_id != guild.id or tournament.is_test:
                    continue
                if not visible_to(member, guild, tournament.channel_id):
                    continue
                events.append(Event('Ultimatum', tournament.channel_id,
                                    tournament.message_id or 0,
                                    'Регистрация' if tournament.status == 'REGISTRATION' else 'Идёт турнир',
                                    tournament.created_by))
        except Exception:
            unavailable = True
            logger.exception('Hub: Ultimatum discovery unavailable')
    return sorted(events, key=lambda e: (e.format, e.channel_id)), unavailable
