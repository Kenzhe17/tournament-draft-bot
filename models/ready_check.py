"""Модель фазы Ready Check («Найден матч») для Ranked Matchmaking."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ReadyCheckStatus(str, Enum):
    """Состояния Ready Check."""

    READY_CHECK = "READY_CHECK"
    ALL_READY = "ALL_READY"      # 8/8, запускаем следующую стадию
    LAUNCHING = "LAUNCHING"      # запуск следующей стадии в процессе
    LAUNCHED = "LAUNCHED"        # турнир создан
    DECLINED = "DECLINED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"      # внутренняя ошибка и пр.


TERMINAL_STATUSES = {
    ReadyCheckStatus.LAUNCHED,
    ReadyCheckStatus.DECLINED,
    ReadyCheckStatus.TIMEOUT,
    ReadyCheckStatus.CANCELLED,
}


@dataclass
class ReadyCheckPlayer:
    """Участник Ready Check."""

    user_id: int
    name: str
    team: str = "A"  # "A" | "B"
    is_ready: bool = False
    ready_at: float = 0.0
    declined: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "team": self.team,
            "is_ready": self.is_ready,
            "ready_at": self.ready_at,
            "declined": self.declined,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReadyCheckPlayer":
        return cls(
            user_id=int(data["user_id"]),
            name=str(data["name"]),
            team=str(data.get("team", "A")),
            is_ready=bool(data.get("is_ready", False)),
            ready_at=float(data.get("ready_at", 0.0)),
            declined=bool(data.get("declined", False)),
        )


@dataclass
class ReadyCheck:
    """Состояние одной проверки готовности найденного матча."""

    match_id: str
    guild_id: int
    search_channel_id: int          # канал MM-поиска (куда вернуть игроков)
    tournament_channel_id: int      # зарезервированный канал турнира
    players: list[ReadyCheckPlayer] = field(default_factory=list)
    status: ReadyCheckStatus = ReadyCheckStatus.READY_CHECK
    deadline: float = 0.0           # unix timestamp
    channel_id: int = 0             # канал сообщения «НАЙДЕН МАТЧ»
    message_id: int = 0
    cancel_reason: str = ""
    created_at: float = field(default_factory=time.time)
    match_num: int = 0              # номер для надписи MATCH #NNNNN

    # ---- derived ----

    @property
    def team_a(self) -> list[ReadyCheckPlayer]:
        return [p for p in self.players if p.team == "A"]

    @property
    def team_b(self) -> list[ReadyCheckPlayer]:
        return [p for p in self.players if p.team == "B"]

    @property
    def ready_count(self) -> int:
        return sum(1 for p in self.players if p.is_ready)

    @property
    def is_active(self) -> bool:
        return self.status == ReadyCheckStatus.READY_CHECK

    def player_by_id(self, user_id: int) -> ReadyCheckPlayer | None:
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    def time_left(self) -> float:
        return max(0.0, self.deadline - time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "guild_id": self.guild_id,
            "search_channel_id": self.search_channel_id,
            "tournament_channel_id": self.tournament_channel_id,
            "players": [p.to_dict() for p in self.players],
            "status": self.status.value,
            "deadline": self.deadline,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "cancel_reason": self.cancel_reason,
            "created_at": self.created_at,
            "match_num": self.match_num,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReadyCheck":
        return cls(
            match_id=str(data["match_id"]),
            guild_id=int(data["guild_id"]),
            search_channel_id=int(data["search_channel_id"]),
            tournament_channel_id=int(data.get("tournament_channel_id", 0)),
            players=[ReadyCheckPlayer.from_dict(p) for p in data.get("players", [])],
            status=ReadyCheckStatus(data.get("status", "READY_CHECK")),
            deadline=float(data.get("deadline", 0.0)),
            channel_id=int(data.get("channel_id", 0)),
            message_id=int(data.get("message_id", 0)),
            cancel_reason=str(data.get("cancel_reason", "")),
            created_at=float(data.get("created_at", time.time())),
            match_num=int(data.get("match_num", 0)),
        )
