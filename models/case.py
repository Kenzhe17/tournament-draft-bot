"""Модели для системы кейсов."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Case:
    """Кейс с предметами."""

    id: str
    name: str
    description: str
    price: int
    drop_rates: dict[str, float]  # rarity: percentage
    is_active: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать в словарь."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "price": self.price,
            "drop_rates": self.drop_rates,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Case":
        """Десериализовать из словаря."""
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            price=data.get("price", 0),
            drop_rates=data.get("drop_rates", {}),
            is_active=data.get("is_active", True),
        )
