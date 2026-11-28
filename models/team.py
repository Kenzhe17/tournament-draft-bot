"""Модель данных команды (Squad Hub 2.0 / BR tournament)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Максимальный основной состав команды
TEAM_MAX_MEMBERS = 4

# Режимы вступления
JOIN_OPEN = "open"                # Открытая — вступает любой
JOIN_APPLICATIONS = "applications"  # По заявкам — капитан подтверждает
JOIN_CLOSED = "closed"            # Закрытая — только по приглашениям
JOIN_PASSWORD = "password"        # По паролю
JOIN_MODES = (JOIN_OPEN, JOIN_APPLICATIONS, JOIN_CLOSED, JOIN_PASSWORD)
JOIN_MODE_LABELS = {
    JOIN_OPEN: "Открытая",
    JOIN_APPLICATIONS: "По заявкам",
    JOIN_CLOSED: "Закрытая",
    JOIN_PASSWORD: "По паролю",
}

# Цели команды
PURPOSE_CASHCUP = "cashcup"
PURPOSE_BR = "br"
PURPOSE_TRAINING = "training"
PURPOSE_ROSTER = "roster"
PURPOSE_CASUAL = "casual"
PURPOSES = (PURPOSE_CASHCUP, PURPOSE_BR, PURPOSE_TRAINING, PURPOSE_ROSTER, PURPOSE_CASUAL)
PURPOSE_LABELS = {
    PURPOSE_CASHCUP: "Cash Cup",
    PURPOSE_BR: "Battle Royale",
    PURPOSE_TRAINING: "Тренировки",
    PURPOSE_ROSTER: "Постоянный состав",
    PURPOSE_CASUAL: "Свободная игра",
}

# Игровые роли (LFG / требования)
PLAY_ROLES = ("Рашер", "Снайпер", "Саппорт", "Капитан", "Универсал")

# Устройства
DEVICES = ("mobile", "pc")
DEVICE_LABELS = {"mobile": "Телефон", "pc": "ПК", "": "Не указано"}

_NAME_MIN, _NAME_MAX = 2, 24
_DESC_MAX = 150


def hash_password(password: str) -> str:
    """PBKDF2-хеш пароля команды: pbkdf2$iter$salt$hash (hex)."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000
    )
    return f"pbkdf2$120000${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль против хеша. Legacy-формат (сырой текст) —
    поддерживается на переходный период: сравнение по совпадению."""
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        # Legacy plaintext (до миграции) — принимаем только точное
        # совпадение; при первом успешном входе хеш мигрирует.
        return secrets.compare_digest(password, stored)
    try:
        _, iters, salt, expected = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt),
            int(iters),
        )
        return secrets.compare_digest(dk.hex(), expected)
    except (ValueError, TypeError):
        return False


def validate_team_name(name: str) -> str | None:
    """None — имя валидно, иначе текст ошибки."""
    name = (name or "").strip()
    if not (_NAME_MIN <= len(name) <= _NAME_MAX):
        return f"Название: от {_NAME_MIN} до {_NAME_MAX} символов"
    return None


def validate_description(desc: str) -> str | None:
    if len(desc or "") > _DESC_MAX:
        return f"Описание: не более {_DESC_MAX} символов"
    return None


@dataclass
class Team:
    """Модель команды для BR турнира / Squad Hub."""

    team_id: str
    team_name: str
    captain_id: int
    guild_id: int
    members: list[int] = field(default_factory=list)  # List of user IDs
    password: str = ""  # legacy plaintext — мигрируется в password_hash
    password_hash: str = ""
    join_mode: str = JOIN_APPLICATIONS
    recruiting: bool = True
    purpose: str = PURPOSE_CASUAL
    description: str = ""
    requirements: dict[str, Any] = field(default_factory=dict)
    logo_url: str = ""
    created_at: str = ""
    updated_at: str = ""
    status_message_id: int | None = None  # Message ID of the status embed
    recruitment_channel_id: int | None = None
    recruitment_message_id: int | None = None

    def __post_init__(self):
        if not self.team_id:
            self.team_id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at
        if self.join_mode not in JOIN_MODES:
            self.join_mode = JOIN_APPLICATIONS
        if self.purpose not in PURPOSES:
            self.purpose = PURPOSE_CASUAL
        # Captain is always a member
        if self.captain_id not in self.members:
            self.members.append(self.captain_id)

    # ---------- сериализация ----------

    def to_dict(self) -> dict[str, Any]:
        """Конвертировать в словарь для сохранения в JSON."""
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "password": "",  # plaintext не сохраняем
            "password_hash": self.password_hash,
            "captain_id": self.captain_id,
            "guild_id": self.guild_id,
            "members": self.members,
            "join_mode": self.join_mode,
            "recruiting": self.recruiting,
            "purpose": self.purpose,
            "description": self.description,
            "requirements": self.requirements,
            "logo_url": self.logo_url,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status_message_id": self.status_message_id,
            "recruitment_channel_id": self.recruitment_channel_id,
            "recruitment_message_id": self.recruitment_message_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Team:
        """Создать объект из словаря из JSON/БД."""
        password = data.get("password", "") or ""
        password_hash = data.get("password_hash", "") or ""
        team = cls(
            team_id=data.get("team_id", ""),
            team_name=data.get("team_name", ""),
            captain_id=data.get("captain_id", 0),
            guild_id=data.get("guild_id", 0),
            members=list(data.get("members", [])),
            password=password,
            password_hash=password_hash,
            join_mode=data.get("join_mode", JOIN_APPLICATIONS),
            recruiting=bool(data.get("recruiting", True)),
            purpose=data.get("purpose", PURPOSE_CASUAL),
            description=data.get("description", ""),
            requirements=data.get("requirements") or {},
            logo_url=data.get("logo_url", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            status_message_id=data.get("status_message_id"),
            recruitment_channel_id=data.get("recruitment_channel_id"),
            recruitment_message_id=data.get("recruitment_message_id"),
        )
        # Миграция legacy-пароля: хешируем при загрузке,
        # plaintext не сохраняем дальше по цепочке.
        if password and not password_hash:
            team.password_hash = hash_password(password)
            team.password = ""
        return team

    # ---------- проверки ----------

    def is_captain(self, user_id: int) -> bool:
        """Проверить, является ли пользователь капитаном."""
        return self.captain_id == user_id

    def is_member(self, user_id: int) -> bool:
        """Проверить, является ли пользователь членом команды."""
        return user_id in self.members

    def add_member(self, user_id: int) -> bool:
        """Добавить члена в команду (in-memory, без сохранения)."""
        if user_id in self.members:
            return False
        if len(self.members) >= TEAM_MAX_MEMBERS:
            return False
        self.members.append(user_id)
        return True

    def remove_member(self, user_id: int) -> bool:
        """Удалить члена из команды (in-memory)."""
        if user_id == self.captain_id:
            return False
        if user_id not in self.members:
            return False
        self.members.remove(user_id)
        return True

    def is_full(self) -> bool:
        """Проверить, заполнена ли команда."""
        return len(self.members) >= TEAM_MAX_MEMBERS

    def free_slots(self) -> int:
        return max(0, TEAM_MAX_MEMBERS - len(self.members))

    def member_count(self) -> int:
        """Получить количество членов команды."""
        return len(self.members)

    @property
    def join_mode_label(self) -> str:
        return JOIN_MODE_LABELS.get(self.join_mode, self.join_mode)

    @property
    def purpose_label(self) -> str:
        return PURPOSE_LABELS.get(self.purpose, self.purpose)

    def check_password(self, password: str) -> bool:
        """Проверить пароль вступления."""
        return verify_password(password, self.password_hash or self.password)

    def set_password(self, password: str) -> None:
        self.password_hash = hash_password(password)
        self.password = ""
