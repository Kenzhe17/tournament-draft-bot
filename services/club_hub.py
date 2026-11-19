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
    tournament_id: str = ""


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
                            tournament.organizer_id, len(tournament.all_players | set(tournament.players_pool)), str(tournament.id)))
    for tournament in br_store.get_by_guild(guild.id):
        phase = getattr(tournament.phase, 'value', tournament.phase)
        if phase == 'complete' or not visible_to(member, guild, tournament.channel_id):
            continue
        events.append(Event('Battle Royale', tournament.channel_id,
                            tournament.message_id or 0,
                            'Регистрация' if phase == 'registration' else 'Идёт турнир',
                            tournament.organizer_id, len(tournament.players), str(tournament.id)))
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
                                    tournament.created_by, tournament_id=str(tournament.id)))
        except Exception:
            unavailable = True
            logger.exception('Hub: Ultimatum discovery unavailable')
    return sorted(events, key=lambda e: (e.format, e.channel_id)), unavailable


async def my_events(guild, member) -> tuple[list[tuple[Event, str]], bool]:
    """Membership is resolved by Discord ID, never by nickname."""
    available, unavailable = await active_events(guild, member)
    entries = []
    for event in available:
        if event.format == 'CS 4×4':
            tournament = store.get(guild.id, event.channel_id)
            if tournament is None:
                continue
            registered = {str(tournament.player_user_ids.get(n))
                          for n in tournament.all_players | set(tournament.players_pool)}
            if str(member.id) not in registered:
                continue
            idx = tournament.get_player_team_index(member.id)
            detail = 'Ожидается формирование команд'
            if idx is not None:
                detail = f"Команда: {tournament.team_names.get(idx) or tournament.teams[idx].get('captain', idx + 1)}"
            entries.append((event, detail))
        elif event.format == 'Battle Royale':
            tournament = br_store.get(guild.id, event.channel_id)
            ids = {str(p.get('id')) for p in tournament.players if isinstance(p, dict)}
            for team in tournament.teams:
                for player in team.get('players', []):
                    if isinstance(player, dict):
                        ids.add(str(player.get('id') or player.get('user_id')))
            if str(member.id) in ids:
                entries.append((event, 'Состав и следующая карта — в карточке турнира'))
        elif DATABASE_URL:
            try:
                from ultimatum.store import ultimatum_store
                # Formats may coexist; list once and match the exact visible event channel.
                for tournament in await ultimatum_store.list_active_all():
                    if tournament.guild_id != guild.id or str(tournament.id) != event.tournament_id:
                        continue
                    entrants = await ultimatum_store.get_entrants(tournament.id)
                    if any(member.id in entrant.member_ids for entrant in entrants):
                        entries.append((event, 'Следующий соперник — в сетке турнира'))
                        break
            except Exception:
                logger.exception('Hub: personal Ultimatum discovery unavailable')
                unavailable = True
    return entries, unavailable


async def managed_events(guild, member):
    from utils.permissions import can_manage_tournament, is_admin_check
    entries, unavailable = await active_events(guild, member)
    managed = []
    for event in entries:
        if event.format == 'CS 4×4':
            tournament = store.get(guild.id, event.channel_id)
            permitted = can_manage_tournament(member, guild, tournament)
        else:
            # Navigation only; actual actions retain format-specific permission checks.
            permitted = member.id == event.organizer_id or is_admin_check(member, guild)
        if permitted:
            managed.append(event)
    return managed, unavailable
