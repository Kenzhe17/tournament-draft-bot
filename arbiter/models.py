"""Доменные модели Arbiter (порт prisma schema -> dataclasses)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from arbiter.enums import (
    Bracket,
    ClaimMode,
    MatchStatus,
    OrgRole,
    TournamentFormat,
    TournamentStatus,
    VetoKind,
    VetoMode,
)


def new_id() -> str:
    """Генератор ID (аналог prisma cuid)."""
    return uuid.uuid4().hex[:25]


def new_public_code(prefix: str = "M") -> str:
    """Публичный код матча/турнира/лобби."""
    import secrets
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Organization:
    id: str = field(default_factory=new_id)
    discord_guild_id: str = ""
    name: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class OrgSettings:
    id: str = field(default_factory=new_id)
    organization_id: str = ""
    admin_role_id: str | None = None
    referee_role_id: str | None = None
    match_category_id: str | None = None
    match_log_channel_id: str | None = None
    evidence_channel_id: str | None = None
    web_permissions: Any = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class UserProfile:
    id: str = field(default_factory=new_id)
    discord_user_id: str = ""
    display_name: str | None = None
    country_code: str | None = None
    bio: str | None = None
    profile_visibility: str = "private"
    open_to_work: bool = False
    avatar_url: str | None = None
    contact_email: str | None = None
    show_contact_email: bool = False
    social_links: Any = None
    game_experiences: list = field(default_factory=list)
    field_roles: list = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class OrgMember:
    id: str = field(default_factory=new_id)
    organization_id: str = ""
    user_profile_id: str = ""
    role: OrgRole = OrgRole.PLAYER
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Team:
    id: str = field(default_factory=new_id)
    organization_id: str = ""
    tournament_id: str | None = None
    name: str = ""
    captain_profile_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class TeamMember:
    id: str = field(default_factory=new_id)
    team_id: str = ""
    user_profile_id: str | None = None
    display_name: str = ""
    team_role: str = "player"
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Match:
    id: str = field(default_factory=new_id)
    public_code: str = field(default_factory=new_public_code)
    organization_id: str = ""
    tournament_id: str | None = None
    channel_id: str | None = None
    control_message_id: str | None = None
    created_by_id: str | None = None
    team_a_name: str = ""
    team_b_name: str = ""
    best_of: int = 1
    rules_preset: str = "generic"
    veto_mode: VetoMode = VetoMode.FINAL_MAP_BAN
    veto_starting_team: str = "teamA"
    veto_timer_seconds: int = 60
    veto_timeout_action: str = "referee_choice"
    character_ban_mode: str = "none"
    character_ban_timer_seconds: int = 30
    character_ban_started_at: datetime | None = None
    character_pool: Any = None
    map_pool: Any = field(default_factory=list)
    status: MatchStatus = MatchStatus.PENDING
    team_a_score: int = 0
    team_b_score: int = 0
    team_a_result: str | None = None
    team_b_result: str | None = None
    allow_player_reports: bool = False
    assigned_referee_id: str | None = None
    referee_claim_mode: ClaimMode = ClaimMode.OPEN
    referee_claim_limit: int = 1
    dispute_reason: str | None = None
    team_a_role_id: str | None = None
    team_b_role_id: str | None = None
    roster_locked_at: datetime | None = None
    roster_locked_by_id: str | None = None
    final_map: str | None = None
    external_ref: str | None = None  # связь с legacy-драфт турниром
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def is_active(self) -> bool:
        return self.status not in (MatchStatus.COMPLETE, MatchStatus.CANCELLED)

    @property
    def maps_to_win(self) -> int:
        return self.best_of // 2 + 1


@dataclass
class MatchParticipant:
    id: str = field(default_factory=new_id)
    match_id: str = ""
    team_id: str = ""
    slot: str = ""  # teamA | teamB


@dataclass
class MatchRefereeAssignment:
    id: str = field(default_factory=new_id)
    match_id: str = ""
    user_profile_id: str = ""
    source: str = "assigned"  # assigned | claimed
    status: str = "active"
    assigned_by_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class VetoAction:
    id: str = field(default_factory=new_id)
    match_id: str = ""
    kind: VetoKind = VetoKind.BAN
    team_slot: str = ""
    map_name: str = ""
    source: str = "team"
    note: str | None = None
    actor_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class CharacterBanAction:
    id: str = field(default_factory=new_id)
    match_id: str = ""
    team_slot: str = ""
    action: str = "ban"
    character: str = ""
    game_role: str | None = None
    source: str = "team"
    note: str | None = None
    actor_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class MapResult:
    id: str = field(default_factory=new_id)
    match_id: str = ""
    map_index: int = 0
    map_name: str = ""
    team_a_score: int = 0
    team_b_score: int = 0
    team_a_result: str | None = None
    team_b_result: str | None = None
    status: str = "pending"
    note: str | None = None
    actor_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class Tournament:
    id: str = field(default_factory=new_id)
    public_code: str = field(default_factory=lambda: new_public_code("T"))
    organization_id: str = ""
    name: str = ""
    game_title: str | None = None
    format: TournamentFormat = TournamentFormat.SINGLE_ELIMINATION
    status: TournamentStatus = TournamentStatus.DRAFT
    best_of: int = 1
    third_place: bool = False
    referee_claim_mode: ClaimMode = ClaimMode.OPEN
    referee_claim_limit: int = 1
    champion_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class TournamentEntry:
    id: str = field(default_factory=new_id)
    tournament_id: str = ""
    team_id: str | None = None
    team_name: str = ""
    seed: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class BracketNode:
    id: str = field(default_factory=new_id)
    tournament_id: str = ""
    bracket: Bracket = Bracket.WINNERS
    round_index: int = 0
    slot_index: int = 0
    label: str | None = None
    best_of: int = 1
    team_a_entry_id: str | None = None
    team_b_entry_id: str | None = None
    team_a_name: str | None = None
    team_b_name: str | None = None
    team_a_source: str | None = None
    team_b_source: str | None = None
    team_a_score: int = 0
    team_b_score: int = 0
    team_a_result: str | None = None
    team_b_result: str | None = None
    winner_slot: str | None = None  # teamA | teamB
    status: str = "pending"  # pending | ready | live | complete | bye
    match_id: str | None = None
    winner_to_node_id: str | None = None
    winner_to_slot: str | None = None
    loser_to_node_id: str | None = None
    loser_to_slot: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class BrLobby:
    id: str = field(default_factory=new_id)
    public_code: str = field(default_factory=lambda: new_public_code("BR"))
    organization_id: str = ""
    channel_id: str | None = None
    control_message_id: str | None = None
    created_by_id: str | None = None
    name: str = ""
    game: str = ""
    status: MatchStatus = MatchStatus.PENDING
    games_planned: int = 6
    kill_points: int = 1
    placement_points: Any = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class BrTeam:
    id: str = field(default_factory=new_id)
    lobby_id: str = ""
    linked_team_id: str | None = None
    name: str = ""
    seed: int | None = None
    discord_role_id: str | None = None
    category_channel_id: str | None = None
    text_channel_id: str | None = None
    voice_channel_id: str | None = None
    team_message_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class BrGameResult:
    id: str = field(default_factory=new_id)
    lobby_id: str = ""
    br_team_id: str = ""
    game_number: int = 0
    placement: int = 0
    kills: int = 0
    points: int = 0
    actor_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class BrAdjustment:
    id: str = field(default_factory=new_id)
    lobby_id: str = ""
    br_team_id: str = ""
    points: int = 0
    kills: int = 0
    game_number: int | None = None
    reason: str = ""
    actor_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class BrLog:
    id: str = field(default_factory=new_id)
    lobby_id: str = ""
    kind: str = "note"  # pause | warning | evidence | note | dispute
    br_team_id: str | None = None
    subject: str | None = None
    game_number: int | None = None
    summary: str | None = None
    details: str | None = None
    rule: str | None = None
    duration_minutes: int | None = None
    attachment_url: str | None = None
    attachment_name: str | None = None
    actor_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class RulesPreset:
    id: str = field(default_factory=new_id)
    organization_id: str = ""
    key: str = ""
    label: str = ""
    game_title: str | None = None
    map_pool: Any = field(default_factory=list)
    character_pool: Any = None
    veto_mode: str = "final_map_ban"
    notes: str | None = None
    created_by_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class RulingPreset:
    id: str = field(default_factory=new_id)
    organization_id: str = ""
    key: str = ""
    label: str = ""
    result_label: str = ""
    default_subject_score: int = 0
    default_opponent_score: int = 0
    applies_to: str = "subject_loses"
    notes: str | None = None
    created_by_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class MatchRoom:
    id: str = field(default_factory=new_id)
    match_id: str = ""
    text_channel_id: str | None = None
    voice_channel_id: str | None = None
    category_id: str | None = None
    player_message_id: str | None = None
    team_a_text_channel_id: str | None = None
    team_a_voice_channel_id: str | None = None
    team_a_message_id: str | None = None
    team_b_text_channel_id: str | None = None
    team_b_voice_channel_id: str | None = None
    team_b_message_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
