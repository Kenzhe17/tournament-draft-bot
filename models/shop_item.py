"""Модели для системы магазина и косметики."""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CosmeticType(str, Enum):
    """Тип косметического предмета."""
    COLOR = "color"  # Цвет текста
    ICON = "icon"  # Значок (эмодзи)
    TAG = "tag"  # Текстовый тег
    FRAME = "frame"  # Рамка профиля


class CosmeticRarity(str, Enum):
    """Редкость косметического предмета."""
    BASIC = "basic"  # Базовый
    PREMIUM = "premium"  # Премиум
    ELITE = "elite"  # Элитный
    SPECIAL = "special"  # Спецэффект


@dataclass
class ShopItem:
    """Товар в магазине."""
    id: str
    name: str
    description: str
    price: int
    cosmetic_type: CosmeticType
    rarity: CosmeticRarity
    value: str  # Значение (hex код цвета, эмодзи, текст тега)
    category: str  # Для группировки в магазине

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать в словарь."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "price": self.price,
            "cosmetic_type": self.cosmetic_type.value,
            "rarity": self.rarity.value,
            "value": self.value,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ShopItem":
        """Десериализовать из словаря."""
        # Handle invalid cosmetic_type values gracefully
        cosmetic_type_str = data.get("cosmetic_type", "color")
        try:
            cosmetic_type = CosmeticType(cosmetic_type_str)
        except ValueError:
            # If the type is invalid, default to ICON
            cosmetic_type = CosmeticType.ICON

        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            price=data.get("price", 0),
            cosmetic_type=cosmetic_type,
            rarity=CosmeticRarity(data.get("rarity", "basic")),
            value=data.get("value", ""),
            category=data.get("category", ""),
        )


@dataclass
class PlayerCosmetic:
    """Косметический предмет игрока."""
    guild_id: int
    user_id: int
    item_id: str
    equipped: bool = False  # Экипирован или нет

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать в словарь."""
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "item_id": self.item_id,
            "equipped": self.equipped,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerCosmetic":
        """Десериализовать из словаря."""
        return cls(
            guild_id=data.get("guild_id", 0),
            user_id=data.get("user_id", 0),
            item_id=data.get("item_id", ""),
            equipped=data.get("equipped", False),
        )
