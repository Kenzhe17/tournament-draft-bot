"""LegacyBridge — мост между arbiter match engine и старым драфт-турниром.

Когда legacy-турнир (драфт 8/16/32) входит в фазу
QUALIFIERS/SEMIFINALS/FINAL, для каждого матча создаётся arbiter Match
с external_ref = "{tournament.id}|{match_type}|{match_index}|{teamA}|{teamB}".
Панель даёт referee-фичи (счёт, доказательства, паузы, варны, комнаты).

При завершении arbiter-матча вызывается ConfirmFinalWinnerButton.callback
— вся статистика/Elo/монеты/история/ставки/прогрессия выполняется
прежним кодом без изменений.
"""

from __future__ import annotations

import logging

import discord

from arbiter.models import Match
from arbiter.panels.match_panel import MatchPanelView, build_match_embed
from arbiter.services.match_service import match_service
from arbiter.services.org_service import org_service

logger = logging.getLogger(__name__)


def _ext_ref(tournament_id: str, match_type: str, match_index: int,
             team_a: int, team_b: int) -> str:
    return f"{tournament_id}|{match_type}|{match_index}|{team_a}|{team_b}"


def _parse_ext_ref(ref: str) -> tuple[str, str, int, int, int] | None:
    parts = ref.split("|")
    if len(parts) != 5:
        return None
    try:
        return parts[0], parts[1], int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None


def _phase_fixtures(tournament) -> list[tuple[str, int, tuple[int, int]]]:
    """Матчи текущей фазы: (match_type, match_index, (teamA_idx, teamB_idx))."""
    from models.tournament import TournamentPhase
    fixtures: list[tuple[str, int, tuple[int, int]]] = []
    if tournament.phase == TournamentPhase.QUALIFIERS:
        for i, m in enumerate(tournament.qualifier_matches):
            fixtures.append(("qualifier", i, (m[0], m[1])))
    elif tournament.phase == TournamentPhase.SEMIFINALS:
        for i, m in enumerate(tournament.semifinal_matches):
            fixtures.append(("semifinal", i, (m[0], m[1])))
    elif tournament.phase == TournamentPhase.FINAL:
        if len(tournament.final_teams) >= 2:
            fixtures.append(("final", 0,
                             (tournament.final_teams[0], tournament.final_teams[1])))
    return fixtures


def _team_display_name(tournament, team_idx: int) -> str:
    team = tournament.teams[team_idx] if team_idx < len(tournament.teams) else {}
    captain = team.get("captain", f"Team {team_idx + 1}")
    return tournament.team_names.get(team_idx, captain)


async def ensure_matches_for_phase(bot, guild: discord.Guild, tournament) -> None:
    """Создать arbiter-матчи + панели для фикстур текущей фазы (идемпотентно)."""
    fixtures = _phase_fixtures(tournament)
    if not fixtures:
        return
    organizer_id = getattr(tournament, "organizer_id", 0) or (
        guild.owner.id if guild.owner else 0)
    member = guild.get_member(organizer_id) if organizer_id else None
    if member is None:
        member = guild.owner
    if member is None:
        return
    try:
        org = await org_service.get_or_create_org(guild, member)
    except Exception as e:
        logger.warning("legacy_bridge: org failed: %s", e)
        return

    channel = guild.get_channel(tournament.channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(tournament.channel_id)
        except discord.HTTPException:
            return
    if not isinstance(channel, discord.TextChannel):
        return

    for match_type, match_index, (team_a, team_b) in fixtures:
        legacy_match_id = f"{tournament.id}_{match_type}_{match_index}"
        if legacy_match_id in getattr(tournament, "resolved_matches", set()):
            continue
        ref = _ext_ref(tournament.id, match_type, match_index, team_a, team_b)
        try:
            existing = await match_service.get_match_by_external_ref(ref)
            if existing and existing.control_message_id:
                continue
            name_a = _team_display_name(tournament, team_a)
            name_b = _team_display_name(tournament, team_b)
            if existing is None:
                existing = await match_service.create_match(
                    org.id, name_a, name_b, best_of=1,
                    channel_id=str(channel.id),
                    created_by_id=str(organizer_id) if organizer_id else None,
                    external_ref=ref,
                )
            view = MatchPanelView(existing.id)
            msg = await channel.send(embed=build_match_embed(existing), view=view)
            bot.add_view(view)
            await match_service.set_control_message(existing, str(channel.id), str(msg.id))
            logger.info("legacy_bridge: arbiter match %s for %s",
                        existing.public_code, ref)
        except Exception as e:
            logger.warning("legacy_bridge: ensure %s failed: %s", ref, e)


# ---------- Fake interaction для программного вызова legacy callback ----------

class _FakePermissions:
    administrator = True


class _FakeUser:
    """Пользователь с правами админа — проходит is_org_check."""

    def __init__(self, real_user):
        self.id = getattr(real_user, "id", 0)
        self.guild_permissions = _FakePermissions()
        self.roles = []
        self.display_name = getattr(real_user, "display_name", "Arbiter")
        self.name = getattr(real_user, "name", "arbiter")
        self.mention = getattr(real_user, "mention", "@arbiter")


class _FakeResponse:
    def __init__(self):
        self._done = False

    async def defer(self, *args, **kwargs):
        self._done = True

    async def send_message(self, *args, **kwargs):
        self._done = True

    async def edit_message(self, *args, **kwargs):
        self._done = True


class _FakeFollowup:
    async def send(self, *args, **kwargs):
        pass


class _FakeInteraction:
    def __init__(self, guild: discord.Guild, client, user):
        self.guild = guild
        self.client = client
        self.user = _FakeUser(user)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def _ensure_temp_stats(tournament, match_type: str, match_index: int,
                       team_a: int, team_b: int) -> None:
    """Заполнить temp_match_stats нулями, если статистика не введена.

    Legacy-flow требует непустую статику перед подтверждением победителя.
    """
    legacy_match_id = f"{tournament.id}_{match_type}_{match_index}"
    stats = tournament.temp_match_stats.get(legacy_match_id)
    if stats:
        return
    players: list[str] = []
    for idx in (team_a, team_b):
        if idx >= len(tournament.teams):
            continue
        team = tournament.teams[idx]
        captain = team.get("captain")
        if captain:
            players.append(captain)
        for c in range(1, 5):
            p = team.get(f"circle{c}")
            if p and p not in players:
                players.append(p)
    tournament.temp_match_stats[legacy_match_id] = {
        p: {"kills": 0, "deaths": 0} for p in players
    }


async def resolve_legacy_match(guild: discord.Guild, client,
                               arb_match: Match, winner_slot: str | None,
                               actor) -> bool:
    """Завершить связанный legacy-матч через старый confirm callback.

    winner_slot: 'teamA' | 'teamB' | None (слот команды в arbiter match).
    """
    if not arb_match.external_ref or winner_slot not in ("teamA", "teamB"):
        return False
    parsed = _parse_ext_ref(arb_match.external_ref)
    if not parsed:
        logger.warning("legacy_bridge: bad external_ref %s", arb_match.external_ref)
        return False
    tournament_id, match_type, match_index, team_a, team_b = parsed

    from storage.json_store import store
    tournament = None
    for t in store.get_by_guild(guild.id):
        if t and t.id == tournament_id:
            tournament = t
            break
    if tournament is None:
        logger.warning("legacy_bridge: tournament %s not found", tournament_id)
        return False

    legacy_match_id = f"{tournament.id}_{match_type}_{match_index}"
    if legacy_match_id in getattr(tournament, "resolved_matches", set()):
        logger.info("legacy_bridge: %s already resolved", legacy_match_id)
        return False

    if match_type == "qualifier":
        pair = tournament.qualifier_matches[match_index]
    elif match_type == "semifinal":
        pair = tournament.semifinal_matches[match_index]
    elif match_type == "final":
        pair = (tournament.final_teams[0], tournament.final_teams[1])
    else:
        return False
    pair = (int(pair[0]), int(pair[1]))
    team_index = pair[0] if winner_slot == "teamA" else pair[1]

    _ensure_temp_stats(tournament, match_type, match_index, pair[0], pair[1])

    from views.matches_view import ConfirmFinalWinnerButton
    team_name = _team_display_name(tournament, team_index)
    btn = ConfirmFinalWinnerButton(
        guild.id, tournament.channel_id, tournament,
        match_type, match_index, team_index, team_name, pair,
    )
    fake = _FakeInteraction(guild, client, actor)
    try:
        await btn.callback(fake)
        # callback сам сохранил fresh-турнир (confirm_* + resolved_matches +
        # store.set) — повторный store.set нашего объекта затёр бы его.
        tournament.resolved_matches.add(legacy_match_id)
        logger.info("legacy_bridge: resolved %s winner=%s", legacy_match_id, team_index)
        return True
    except Exception as e:
        logger.error("legacy_bridge: resolve failed for %s: %s", legacy_match_id, e,
                     exc_info=True)
        return False
