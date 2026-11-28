"""Модель данных команды для BR турнира."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def hash_password(password: str) -> str:
    """PBKDF2-хеш пароля команды: pbkdf2$iter$salt$hash (hex).

    Rollback-compat (Squad Hub): пароль хранится хешем в колонке
    ``password_hash``; plaintext-колонка ``password`` остаётся
    пустой. Хеш совместим с форматом Squad Hub 2.0 — повторное
    обновление до Squad Hub читает его без миграции."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt),
        120_000)
    return f"pbkdf2$120000${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль против хеша. Legacy-формат (сырой текст)
    принимается на переходный период — старые команды до
    миграции Squad Hub хранят plaintext."""
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        return secrets.compare_digest(password, stored)
    try:
        _, iters, salt, expected = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt), int(iters))
        return secrets.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False


@dataclass
class Team:
    """Модель команды для BR турнира."""

    team_id: str
    team_name: str
    password: str
    captain_id: int
    guild_id: int
    members: list[int] = field(default_factory=list)  # List of user IDs
    password_hash: str = ""  # PBKDF2-хеш (Squad Hub совместимость)
    created_at: str = ""
    status_message_id: int | None = None  # Message ID of the status embed

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
            "password_hash": self.password_hash,
            "captain_id": self.captain_id,
            "guild_id": self.guild_id,
            "members": self.members,
            "created_at": self.created_at,
            "status_message_id": self.status_message_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Team:
        """Создать объект из словаря из JSON."""
        return cls(
            team_id=data.get("team_id", ""),
            team_name=data.get("team_name", ""),
            password=data.get("password", ""),
            password_hash=data.get("password_hash", "") or "",
            captain_id=data.get("captain_id", 0),
            guild_id=data.get("guild_id", 0),
            members=data.get("members", []),
            created_at=data.get("created_at", ""),
            status_message_id=data.get("status_message_id"),
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

    def check_password(self, password: str) -> bool:
        """Проверить пароль вступления: хеш (Squad Hub) или
        legacy plaintext — без восстановления plaintext."""
        return verify_password(
            password, self.password_hash or self.password)

    def set_password(self, password: str) -> None:
        """Установить пароль: хранится только PBKDF2-хеш,
        plaintext не сохраняется."""
        self.password_hash = hash_password(password)
        self.password = ""
