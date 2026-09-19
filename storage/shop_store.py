"""Storage для системы магазина и косметики."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from models.shop_item import ShopItem, PlayerCosmetic

if TYPE_CHECKING:
    pass

DATA_DIR = Path("data")
SHOP_FILE = DATA_DIR / "shop_items.json"
INVENTORY_FILE = DATA_DIR / "player_inventory.json"


class ShopStore:
    """Хранилище товаров магазина."""

    def __init__(self) -> None:
        self._items: dict[str, ShopItem] = {}  # item_id -> ShopItem
        self._use_db = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить товары из файла."""
        if not SHOP_FILE.exists():
            return

        try:
            with open(SHOP_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item_id, item_data in data.items():
                    self._items[item_id] = ShopItem.from_dict(item_data)
        except (json.JSONDecodeError, KeyError):
            self._items = {}

    def save(self) -> None:
        """Сохранить товары в файл."""
        data = {
            item_id: item.to_dict()
            for item_id, item in self._items.items()
        }
        with open(SHOP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_item(self, item_id: str) -> ShopItem | None:
        """Получить товар по ID."""
        return self._items.get(item_id)

    def get_all_items(self) -> list[ShopItem]:
        """Получить все товары."""
        return list(self._items.values())

    def get_items_by_category(self, category: str) -> list[ShopItem]:
        """Получить товары по категории."""
        return [item for item in self._items.values() if item.category == category]

    def add_item(self, item: ShopItem) -> None:
        """Добавить товар."""
        self._items[item.id] = item
        self.save()

    def enable_db(self) -> None:
        """Включить режим базы данных."""
        self._use_db = True


class InventoryStore:
    """Хранилище инвентаря игроков."""

    def __init__(self) -> None:
        self._inventory: dict[str, list[PlayerCosmetic]] = {}  # guild_id:user_id -> list of cosmetics
        self._use_db = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить инвентарь из файла."""
        if not INVENTORY_FILE.exists():
            return

        try:
            with open(INVENTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for key, cosmetics_data in data.items():
                    self._inventory[key] = [PlayerCosmetic.from_dict(c) for c in cosmetics_data]
        except (json.JSONDecodeError, KeyError):
            self._inventory = {}

    def save(self) -> None:
        """Сохранить инвентарь в файл."""
        data = {
            key: [cosmetic.to_dict() for cosmetic in cosmetics]
            for key, cosmetics in self._inventory.items()
        }
        with open(INVENTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _get_key(self, guild_id: int, user_id: int) -> str:
        """Получить ключ для хранения."""
        return f"{guild_id}:{user_id}"

    def get_player_inventory(self, guild_id: int, user_id: int) -> list[PlayerCosmetic]:
        """Получить инвентарь игрока."""
        key = self._get_key(guild_id, user_id)
        return self._inventory.get(key, [])

    def add_cosmetic(self, cosmetic: PlayerCosmetic) -> None:
        """Добавить косметический предмет в инвентарь."""
        key = self._get_key(cosmetic.guild_id, cosmetic.user_id)
        if key not in self._inventory:
            self._inventory[key] = []

        # Проверяем есть ли уже такой предмет
        for existing in self._inventory[key]:
            if existing.item_id == cosmetic.item_id:
                return  # Уже есть

        self._inventory[key].append(cosmetic)
        self.save()

    def equip_cosmetic(self, guild_id: int, user_id: int, item_id: str) -> bool:
        """Экипировать косметический предмет."""
        key = self._get_key(guild_id, user_id)
        if key not in self._inventory:
            return False

        # Найти предмет и его тип
        target_cosmetic = None
        target_type = None
        for cosmetic in self._inventory[key]:
            if cosmetic.item_id == item_id:
                target_cosmetic = cosmetic
                # Получить тип предмета
                item = shop_store.get_item(item_id)
                if item:
                    target_type = item.cosmetic_type.value
                break

        if not target_cosmetic or not target_type:
            return False

        # Снять все предметы того же типа (кроме текущего)
        for cosmetic in self._inventory[key]:
            if cosmetic.item_id != item_id:
                item = shop_store.get_item(cosmetic.item_id)
                if item and item.cosmetic_type.value == target_type:
                    cosmetic.equipped = False

        # Экипировать текущий предмет
        target_cosmetic.equipped = True
        self.save()
        return True

    def unequip_cosmetic(self, guild_id: int, user_id: int, item_id: str) -> bool:
        """Снять косметический предмет."""
        key = self._get_key(guild_id, user_id)
        if key not in self._inventory:
            return False

        for cosmetic in self._inventory[key]:
            if cosmetic.item_id == item_id:
                cosmetic.equipped = False
                self.save()
                return True

        return False

    def get_equipped_cosmetics(self, guild_id: int, user_id: int) -> list[PlayerCosmetic]:
        """Получить экипированные косметические предметы."""
        inventory = self.get_player_inventory(guild_id, user_id)
        return [cosmetic for cosmetic in inventory if cosmetic.equipped]

    def enable_db(self) -> None:
        """Включить режим базы данных."""
        self._use_db = True


# Глобальные экземпляры
shop_store = ShopStore()
inventory_store = InventoryStore()
