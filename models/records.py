"""Модель исторических рекордов."""

from dataclasses import dataclass
from typing import Any
from datetime import datetime


@dataclass
class Record:
    """Исторический рекорд."""
    record_type: str  # Тип рекорда (например: "max_kills", "most_coins")
    player_name: str  # Имя игрока
    user_id: int  # ID игрока
    value: int  # Значение рекорда
    guild_id: int  # ID сервера
    timestamp: str  # Время установления рекорда (ISO format)

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "record_type": self.record_type,
            "player_name": self.player_name,
            "user_id": self.user_id,
            "value": self.value,
            "guild_id": self.guild_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        """Десериализация из словаря."""
        return cls(
            record_type=data.get("record_type", ""),
            player_name=data.get("player_name", ""),
            user_id=data.get("user_id", 0),
            value=data.get("value", 0),
            guild_id=data.get("guild_id", 0),
            timestamp=data.get("timestamp", ""),
        )


class RecordStore:
    """Хранилище исторических рекордов."""

    def __init__(self):
        self._records: dict[str, Record] = {}  # Key: "guild_id:record_type", Value: Record

    def get_record(self, guild_id: int, record_type: str) -> Record | None:
        """Получить рекорд по типу."""
        key = f"{guild_id}:{record_type}"
        return self._records.get(key)

    def set_record(self, record: Record) -> None:
        """Установить рекорд."""
        key = f"{record.guild_id}:{record.record_type}"
        self._records[key] = record

    def get_all_records(self, guild_id: int) -> list[Record]:
        """Получить все рекорды сервера."""
        return [r for r in self._records.values() if r.guild_id == guild_id]

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {key: record.to_dict() for key, record in self._records.items()}

    def from_dict(self, data: dict[str, Any]) -> None:
        """Десериализация из словаря."""
        self._records = {key: Record.from_dict(value) for key, value in data.items()}


record_store = RecordStore()
