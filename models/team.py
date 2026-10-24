"""Модель данных команды для BR турнира."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Team:
    """Модель команды для BR турнира."""

    team_id: str
    team_name: str
    password: str
    captain_id: int
    guild_id: int
    members: list[int] = field(default_factory=list)  # List of user IDs
    created_at: str = ""

    def __post_init__(self):
        if not self.team_id:
            self.team_id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        # Captain is always a member
        if self.captain_id not in self.members:
            self.members.append(self.captain_id)

    def to_dict(self) -> dict[str, Any]:
        """Конвертировать в словарь для сохранения в JSON."""
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "password": self.password,
            "captain_id": self.captain_id,
            "guild_id": self.guild_id,
            "members": self.members,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Team:
        """Создать объект из словаря из JSON."""
        return cls(
            team_id=data.get("team_id", ""),
            team_name=data.get("team_name", ""),
            password=data.get("password", ""),
            captain_id=data.get("captain_id", 0),
            guild_id=data.get("guild_id", 0),
            members=data.get("members", []),
            created_at=data.get("created_at", ""),
        )

    def is_captain(self, user_id: int) -> bool:
        """Проверить, является ли пользователь капитаном."""
        return self.captain_id == user_id

    def is_member(self, user_id: int) -> bool:
        """Проверить, является ли пользователь членом команды."""
        return user_id in self.members

    def add_member(self, user_id: int) -> bool:
        """Добавить члена в команду.

        Args:
            user_id: Discord ID пользователя

        Returns:
            True если успешно, False если уже в команде или лимит превышен
        """
        if user_id in self.members:
            return False
        if len(self.members) >= 4:
            return False
        self.members.append(user_id)
        return True

    def remove_member(self, user_id: int) -> bool:
        """Удалить члена из команды.

        Args:
            user_id: Discord ID пользователя

        Returns:
            True если успешно, False если пользователь не найден или это капитан
        """
        if user_id == self.captain_id:
            return False
        if user_id not in self.members:
            return False
        self.members.remove(user_id)
        return True

    def is_full(self) -> bool:
        """Проверить, заполнена ли команда (4 игрока)."""
        return len(self.members) >= 4

    def member_count(self) -> int:
        """Получить количество членов команды."""
        return len(self.members)
