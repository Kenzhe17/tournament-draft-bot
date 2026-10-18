"""Модели для системы питомцев."""

from dataclasses import dataclass
from enum import Enum
from typing import Any
from datetime import datetime


class PetRarity(str, Enum):
    """Редкость питомца."""
    COMMON = "common"  # Обычный
    UNCOMMON = "uncommon"  # Необычный
    EPIC = "epic"  # Эпический
    LEGENDARY = "legendary"  # Легендарный


class PetType(str, Enum):
    """Тип питомца."""
    GOLDEN_FOX = "golden_fox"  # Золотой лис
    SCOUT_FOX = "scout_fox"  # Лис-разведчик
    TREASURY_OWL = "treasury_owl"  # Сова-казначей
    RACCOON_ROBBER = "raccoon_robber"  # Енот-грабитель
    RAVEN_SMUGGLER = "raven_smuggler"  # Ворон-контрабандист
    MIMIC_COLLECTOR = "mimic_collector"  # Мимик-собиратель
    GUARDIAN_TURTLE = "guardian_turtle"  # Черепаха-хранитель
    ARENA_WOLF = "arena_wolf"  # Волк арены


class AbilityType(str, Enum):
    """Тип способности."""
    CASE_COIN_BONUS = "case_coin_bonus"  # Бонус к монетам из кейсов
    PAY_FEE_REDUCTION = "pay_fee_reduction"  # Снижение комиссии pay
    ROB_SUCCESS_BONUS = "rob_success_bonus"  # Бонус к шансу ограбления
    ROB_JAIL_REDUCTION = "rob_jail_reduction"  # Сокращение тюрьмы
    ROB_LOSS_REDUCTION = "rob_loss_reduction"  # Снижение штрафа при неудаче
    ROB_DEFENSE = "rob_defense"  # Защита от ограбления
    ROB_ITEM_PROTECTION = "rob_item_protection"  # Защита предмета от кражи
    ROB_LOOT_BONUS = "rob_loot_bonus"  # Бонус к добыче при ограблении
    SELL_FEE_REDUCTION = "sell_fee_reduction"  # Снижение комиссии продажи
    CASE_EXTRA_REWARD = "case_extra_reward"  # Дополнительная награда из кейса
    ITEM_PROTECTION = "item_protection"  # Защита выбранного предмета
    ELO_WIN_BONUS = "elo_win_bonus"  # Бонус к Elo за победу
    ELO_LOSS_REDUCTION = "elo_loss_reduction"  # Снижение потери Elo


@dataclass
class PetAbility:
    """Способность питомца."""
    name: str
    description: str
    ability_type: AbilityType
    max_value: float  # Максимальное значение (процент или процентные пункты)
    energy_cost: int  # Затраты энергии

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать в словарь."""
        return {
            "name": self.name,
            "description": self.description,
            "ability_type": self.ability_type.value,
            "max_value": self.max_value,
            "energy_cost": self.energy_cost,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PetAbility":
        """Десериализовать из словаря."""
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            ability_type=AbilityType(data.get("ability_type", "case_coin_bonus")),
            max_value=data.get("max_value", 0.0),
            energy_cost=data.get("energy_cost", 5),
        )


@dataclass
class PetDefinition:
    """Определение питомца (каталог)."""
    pet_type: PetType
    name: str
    rarity: PetRarity
    description: str
    specialization: str
    image_url: str
    price: int
    ability_1: PetAbility
    ability_2: PetAbility

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать в словарь."""
        return {
            "pet_type": self.pet_type.value,
            "name": self.name,
            "rarity": self.rarity.value,
            "description": self.description,
            "specialization": self.specialization,
            "image_url": self.image_url,
            "price": self.price,
            "ability_1": self.ability_1.to_dict(),
            "ability_2": self.ability_2.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PetDefinition":
        """Десериализовать из словаря."""
        return cls(
            pet_type=PetType(data.get("pet_type", "golden_fox")),
            name=data.get("name", ""),
            rarity=PetRarity(data.get("rarity", "common")),
            description=data.get("description", ""),
            specialization=data.get("specialization", ""),
            image_url=data.get("image_url", ""),
            price=data.get("price", 0),
            ability_1=PetAbility.from_dict(data.get("ability_1", {})),
            ability_2=PetAbility.from_dict(data.get("ability_2", {})),
        )


@dataclass
class Pet:
    """Питомец игрока."""
    pet_id: int
    owner_1_id: int  # Основной владелец
    owner_2_id: int | None  # Совладелец (может быть None)
    pet_type: PetType
    rarity: PetRarity
    pet_name: str
    level: int
    exp: int
    fullness: int  # Сытость (0-100)
    energy: int  # Энергия (0-100)
    last_fed_at: datetime  # Время последнего кормления
    last_energy_update_at: datetime  # Время последнего обновления энергии
    created_at: datetime
    protected_item_id: str | None = None  # ID защищаемого предмета (для Черепахи)
    hunger_started_at: datetime | None = None  # Время когда сытость достигла 0

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать в словарь."""
        return {
            "pet_id": self.pet_id,
            "owner_1_id": self.owner_1_id,
            "owner_2_id": self.owner_2_id,
            "pet_type": self.pet_type.value,
            "rarity": self.rarity.value,
            "pet_name": self.pet_name,
            "level": self.level,
            "exp": self.exp,
            "fullness": self.fullness,
            "energy": self.energy,
            "last_fed_at": self.last_fed_at.isoformat(),
            "last_energy_update_at": self.last_energy_update_at.isoformat(),
            "created_at": self.created_at.isoformat(),
            "protected_item_id": self.protected_item_id,
            "hunger_started_at": self.hunger_started_at.isoformat() if self.hunger_started_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Pet":
        """Десериализовать из словаря."""
        return cls(
            pet_id=data.get("pet_id", 0),
            owner_1_id=data.get("owner_1_id", 0),
            owner_2_id=data.get("owner_2_id"),
            pet_type=PetType(data.get("pet_type", "golden_fox")),
            rarity=PetRarity(data.get("rarity", "common")),
            pet_name=data.get("pet_name", ""),
            level=data.get("level", 1),
            exp=data.get("exp", 0),
            fullness=data.get("fullness", 100),
            energy=data.get("energy", 100),
            last_fed_at=_parse_datetime(data.get("last_fed_at")),
            last_energy_update_at=_parse_datetime(data.get("last_energy_update_at")),
            created_at=_parse_datetime(data.get("created_at")),
            protected_item_id=data.get("protected_item_id"),
            hunger_started_at=_parse_datetime(data.get("hunger_started_at")),
        )

    def get_exp_to_next_level(self) -> int:
        """Получить опыт до следующего уровня."""
        exp_requirements = {
            1: 50,
            2: 100,
            3: 160,
            4: 230,
            5: 310,
            6: 400,
            7: 500,
            8: 610,
            9: 730,
            10: 0,  # Максимальный уровень
        }
        return exp_requirements.get(self.level, 0)


def _parse_datetime(value):
    """Парсит datetime из строки или возвращает как есть если уже datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value

    def is_max_level(self) -> bool:
        """Возраст не ограничен."""
        return False

    def can_use_ability(self, ability_level: int) -> bool:
        """Способности открыты с 1 возраста."""
        return True

    def get_ability_effectiveness(self) -> float:
        """Способности всегда работают на полную мощность."""
        return 1.0


# Опыт для перехода между уровнями
EXP_REQUIREMENTS = {
    1: 50,
    2: 100,
    3: 160,
    4: 230,
    5: 310,
    6: 400,
    7: 500,
    8: 610,
    9: 730,
    10: 0,  # Максимальный уровень
}

# Цены по редкости
RARITY_PRICES = {
    PetRarity.COMMON: 3000,
    PetRarity.UNCOMMON: 8000,
    PetRarity.EPIC: 15000,
    PetRarity.LEGENDARY: 30000,
}

# Затраты энергии по действию
ENERGY_COSTS = {
    "case_open": 6,
    "pay": 6,
    "rob": 10,
    "rob_defense": 9,
    "elo_match": 15,
    "sell": 8,
    "gift": 8,
}
