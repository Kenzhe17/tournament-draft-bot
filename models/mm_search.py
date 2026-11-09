"""Модель для поиска игроков в matchmaking."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MMFormat(str, Enum):
    """Формат matchmaking."""
    DUO = "duo"  # 2x2, 4 игрока
    SQUAD = "squad"  # 4x4, 8 игроков


# Тип очереди Ranked Matchmaking
QUEUE_TYPE_NORMAL = "normal"    # Обычный Ranked — без ограничений по SR
QUEUE_TYPE_NO_RUIN = "no_ruin"  # «БЕЗ РУИН» — SR 5.5–10.0
NO_RUIN_MIN_SR = 5.5
NO_RUIN_MAX_SR = 10.0


# Состояния очереди Ranked Matchmaking
QUEUE_OPEN = "open"                    # принимает игроков
QUEUE_LOCKED_READY_CHECK = "locked"    # матч найден, идёт ready-check
QUEUE_WAITING_CHANNEL = "waiting_channel"  # матч готов, ждёт свободный канал


@dataclass
class MMSearch:
    """Состояние поиска игроков."""

    guild_id: int
    channel_id: int
    message_id: int = 0
    format: MMFormat = MMFormat.SQUAD
    players: list[str] = field(default_factory=list)  # Список имен игроков
    player_user_ids: dict[str, int] = field(default_factory=dict)  # name -> user_id
    created_at: str = field(default_factory=lambda: __import__('datetime').datetime.now().isoformat())
    timer_started: bool = False  # Таймер запущен?
    tournament_channel_id: int = 0  # Канал в котором будет создан турнир
    state: str = QUEUE_OPEN        # open | locked
    match_num: int = 0             # номер матча на время ready-check
    queue_type: str = QUEUE_TYPE_NORMAL  # normal | no_ruin

    @property
    def required_players(self) -> int:
        """Требуемое количество игроков."""
        return 4 if self.format == MMFormat.DUO else 8

    @property
    def is_full(self) -> bool:
        """Заполнен ли поиск."""
        return len(self.players) >= self.required_players

    @property
    def is_locked(self) -> bool:
        """Очередь закрыта на время ready-check или ожидания канала."""
        return self.state in (
            QUEUE_LOCKED_READY_CHECK, QUEUE_WAITING_CHANNEL,
        )

    @property
    def is_waiting_channel(self) -> bool:
        return self.state == QUEUE_WAITING_CHANNEL

    def add_player(self, name: str, user_id: int) -> bool:
        """Добавить игрока в поиск. Возвращает True если добавлен."""
        if name in self.players:
            return False
        if len(self.players) >= self.required_players:
            return False
        self.players.append(name)
        self.player_user_ids[name] = user_id
        return True

    def remove_player(self, name: str) -> bool:
        """Удалить игрока из поиска. Возвращает True если удален."""
        if name not in self.players:
            return False
        self.players.remove(name)
        if name in self.player_user_ids:
            del self.player_user_ids[name]
        return True

    def clear(self) -> None:
        """Очистить поиск и вернуть очередь в открытое состояние."""
        self.players = []
        self.player_user_ids = {}
        self.timer_started = False
        self.tournament_channel_id = 0
        self.state = QUEUE_OPEN
        self.match_num = 0

    def to_dict(self) -> dict[str, Any]:
        """Конвертировать в словарь для JSON."""
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "format": self.format.value,
            "players": self.players,
            "player_user_ids": self.player_user_ids,
            "created_at": self.created_at,
            "timer_started": self.timer_started,
            "tournament_channel_id": self.tournament_channel_id,
            "state": self.state,
            "match_num": self.match_num,
            "queue_type": self.queue_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MMSearch":
        """Создать из словаря JSON."""
        return cls(
            guild_id=data["guild_id"],
            channel_id=data["channel_id"],
            message_id=data.get("message_id", 0),
            format=MMFormat(data.get("format", "squad")),
            players=data.get("players", []),
            player_user_ids=data.get("player_user_ids", {}),
            created_at=data.get("created_at", ""),
            timer_started=data.get("timer_started", False),
            tournament_channel_id=data.get("tournament_channel_id", 0),
            state=data.get("state", QUEUE_OPEN),
            match_num=int(data.get("match_num", 0) or 0),
            queue_type=data.get("queue_type", QUEUE_TYPE_NORMAL),
        )
