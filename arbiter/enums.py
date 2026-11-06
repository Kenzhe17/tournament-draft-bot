"""Доменные перечисления Arbiter (порт prisma enums)."""

from __future__ import annotations

import enum


class OrgRole(str, enum.Enum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    HEAD_REF = "HEAD_REF"
    REFEREE = "REFEREE"
    PLAYER = "PLAYER"


class MatchStatus(str, enum.Enum):
    PENDING = "PENDING"
    VETO = "VETO"
    LIVE = "LIVE"
    DISPUTED = "DISPUTED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"


class VetoKind(str, enum.Enum):
    BAN = "BAN"
    PICK = "PICK"


class Bracket(str, enum.Enum):
    WINNERS = "winners"
    LOSERS = "losers"
    GRAND_FINAL = "grand_final"
    THIRD_PLACE = "third_place"
    ROUND_ROBIN = "round_robin"


class TournamentStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETE = "complete"


class TournamentFormat(str, enum.Enum):
    SINGLE_ELIMINATION = "single_elimination"
    DOUBLE_ELIMINATION = "double_elimination"
    ROUND_ROBIN = "round_robin"


class ClaimMode(str, enum.Enum):
    OPEN = "open"
    ASSIGNED = "assigned"
    TOURNAMENT = "tournament"


class VetoMode(str, enum.Enum):
    FINAL_MAP_BAN = "final_map_ban"
    SERIES_PICKS = "series_picks"
    MANUAL = "manual"


class RefAssignmentStatus(str, enum.Enum):
    ACTIVE = "active"
    RELEASED = "released"


class EvidenceStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    REVIEWED = "reviewed"
    REJECTED = "rejected"


class ScoreReportStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RosterStatus(str, enum.Enum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class PauseType(str, enum.Enum):
    TEAM = "team"
    TECHNICAL = "technical"
    ADMIN = "admin"
    TACTICAL = "tactical"
    EMERGENCY = "emergency"
    OTHER = "other"


class RulingResult(str, enum.Enum):
    """Типы решений судьи (rulings)."""

    WIN = "win"
    LOSS = "loss"
    FORFEIT = "forfeit"
    DQ = "dq"
    NO_SHOW = "no_show"
    ADMIN_LOSS = "admin_loss"
    DRAW = "draw"
    CANCELLED = "cancelled"


class AnnouncementTarget(str, enum.Enum):
    EVERYONE = "everyone"
    STAFF = "staff"
    PLAYERS = "players"
    ORG_ROLE = "org_role"
    TEAM_ROLE = "team_role"
