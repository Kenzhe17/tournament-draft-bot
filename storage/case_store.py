"""Storage для системы кейсов."""

import json
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

from models.case import Case
from storage.shop_store import shop_store
from storage.user_balance_store import user_balance_store
from storage.shop_store import inventory_store

if TYPE_CHECKING:
    pass

DATA_DIR = Path("data")
CASES_FILE = DATA_DIR / "cases.json"
CASE_HISTORY_FILE = DATA_DIR / "case_history.json"


class CaseStore:
    """Хранилище кейсов."""

    def __init__(self) -> None:
        self._cases: dict[str, Case] = {}  # case_id -> Case
        self._use_db = True  # Использовать PostgreSQL
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить кейсы из файла."""
        if not CASES_FILE.exists():
            self._initialize_default_cases()
            return

        try:
            with open(CASES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for case_id, case_data in data.items():
                    self._cases[case_id] = Case.from_dict(case_data)
            
            # Проверить и обновить старые drop_rates
            self._update_drop_rates_if_needed()
        except (json.JSONDecodeError, KeyError):
            self._initialize_default_cases()

    def _update_drop_rates_if_needed(self) -> None:
        """Обновить drop_rates если они старого формата."""
        updated = False
        
        # Стандартные новые drop_rates
        new_rates = {
            "item": 0.10,
            "nothing": 0.40,
            "coins_tiers": [0.50, 0.25, 0.20, 0.10, 0.05]
        }
        
        for case_id, case in self._cases.items():
            # Проверить если nothing не 0.40, обновить
            current_nothing = case.drop_rates.get("nothing", 0.0)
            if abs(current_nothing - 0.40) > 0.01:  # Если не 40%
                # Пересчитать drop_rates
                item_key = None
                nothing_key = "nothing"
                coin_keys = []
                
                for key in case.drop_rates.keys():
                    if key.startswith("item_"):
                        item_key = key
                    elif key.startswith("coins_"):
                        coin_keys.append(key)
                
                # Новые значения
                new_drop_rates = {}
                if item_key:
                    new_drop_rates[item_key] = 0.10
                new_drop_rates[nothing_key] = 0.40
                
                # Распределить монеты по 5 уровням
                if coin_keys:
                    coin_keys.sort()
                    for i, coin_key in enumerate(coin_keys):
                        if i < len(new_rates["coins_tiers"]):
                            new_drop_rates[coin_key] = new_rates["coins_tiers"][i]
                
                case.drop_rates = new_drop_rates
                updated = True
        
        if updated:
            self.save()

    def save(self) -> None:
        """Сохранить кейсы в файл."""
        data = {
            case_id: case.to_dict()
            for case_id, case in self._cases.items()
        }
        with open(CASES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _initialize_default_cases(self) -> None:
        """Инициализировать кейсы по умолчанию."""
        self._cases = {
            "basic": Case(
                id="basic",
                name="📦 Basic Case",
                description="Базовые предметы",
                price=200,
                drop_rates={
                    "item_basic": 0.10,
                    "coins_100": 0.50,
                    "coins_200": 0.25,
                    "coins_300": 0.20,
                    "coins_400": 0.10,
                    "coins_500": 0.05,
                    "nothing": 0.40,
                },
                is_active=True
            ),
            "premium": Case(
                id="premium",
                name="🎁 Premium Case",
                description="Редкие предметы",
                price=500,
                drop_rates={
                    "item_premium": 0.10,
                    "coins_250": 0.50,
                    "coins_500": 0.25,
                    "coins_750": 0.20,
                    "coins_1000": 0.10,
                    "coins_1250": 0.05,
                    "nothing": 0.40,
                },
                is_active=True
            ),
            "elite": Case(
                id="elite",
                name="💎 Elite Case",
                description="Легендарные предметы",
                price=1000,
                drop_rates={
                    "item_elite": 0.10,
                    "coins_500": 0.50,
                    "coins_1000": 0.25,
                    "coins_1500": 0.20,
                    "coins_2000": 0.10,
                    "coins_2500": 0.05,
                    "nothing": 0.40,
                },
                is_active=True
            ),
            "special": Case(
                id="special",
                name="✨ Special Case",
                description="Эксклюзивные предметы",
                price=2000,
                drop_rates={
                    "item_special": 0.10,
                    "coins_1000": 0.50,
                    "coins_2000": 0.25,
                    "coins_3000": 0.20,
                    "coins_4000": 0.10,
                    "coins_5000": 0.05,
                    "nothing": 0.40,
                },
                is_active=True
            ),
        }
        self.save()

    def get_case(self, case_id: str) -> Case | None:
        """Получить кейс по ID."""
        return self._cases.get(case_id)

    def get_all_cases(self) -> list[Case]:
        """Получить все кейсы."""
        return [case for case in self._cases.values() if case.is_active]

    def add_case(self, case: Case) -> None:
        """Добавить кейс."""
        self._cases[case.id] = case
        self.save()

    async def open_case(self, guild_id: int, user_id: int, case_id: str, guild) -> dict[str, Any]:
        """Открыть кейс.

        Args:
            guild_id: ID сервера
            user_id: ID пользователя
            case_id: ID кейса
            guild: Discord guild object

        Returns:
            Словарь с результатом: {"type": "item"/"coins"/"nothing", "value": ..., "rarity": ...}
        """
        case = self.get_case(case_id)
        if not case:
            return {"type": "error", "value": "Кейс не найден"}

        # Проверить баланс
        balance = await user_balance_store.get_balance(guild_id, user_id)
        if balance < case.price:
            return {"type": "error", "value": "Недостаточно монет"}

        # Снять стоимость
        await user_balance_store.subtract_balance(guild_id, user_id, case.price)

        # Выбрать результат по drop rates
        roll = random.random()
        cumulative = 0.0

        for drop_type, rate in case.drop_rates.items():
            cumulative += rate
            if roll <= cumulative:
                break
        else:
            drop_type = "nothing"

        # Обработать результат
        if drop_type == "nothing":
            result = {"type": "nothing", "value": "Ничего", "rarity": "common"}
        elif drop_type.startswith("coins_"):
            coins_amount = int(drop_type.split("_")[1])
            await user_balance_store.add_balance(guild_id, user_id, coins_amount)
            result = {"type": "coins", "value": coins_amount, "rarity": "common"}
        elif drop_type.startswith("item_"):
            # Выбрать случайный предмет из категории
            category = drop_type.replace("item_", "")
            items = shop_store.get_items_by_category(category)
            if items:
                item = random.choice(items)
                # Добавить в инвентарь
                from models.shop_item import PlayerCosmetic
                cosmetic = PlayerCosmetic(
                    guild_id=guild_id,
                    user_id=user_id,
                    item_id=item.id,
                    equipped=False
                )
                inventory_store.add_cosmetic(cosmetic)
                result = {"type": "item", "value": item, "rarity": item.rarity.value}
            else:
                # Если нет предметов в категории, выдать монеты
                fallback_coins = case.price // 2
                await user_balance_store.add_balance(guild_id, user_id, fallback_coins)
                result = {"type": "coins", "value": fallback_coins, "rarity": "common"}
        else:
            result = {"type": "nothing", "value": "Ничего", "rarity": "common"}

        # Записать в историю
        self._add_to_history(guild_id, user_id, case_id, result)

        return result

    def _add_to_history(self, guild_id: int, user_id: int, case_id: str, result: dict[str, Any]) -> None:
        """Добавить запись в историю открытий."""
        history = self._load_history()
        key = f"{guild_id}:{user_id}"

        if key not in history:
            history[key] = []

        history[key].append({
            "case_id": case_id,
            "result": result,
            "opened_at": str(__import__("datetime").datetime.now())
        })

        # Хранить только последние 50 открытий
        if len(history[key]) > 50:
            history[key] = history[key][-50:]

        self._save_history(history)

    def _load_history(self) -> dict:
        """Загрузить историю из файла."""
        if not CASE_HISTORY_FILE.exists():
            return {}

        try:
            with open(CASE_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError):
            return {}

    def _save_history(self, history: dict) -> None:
        """Сохранить историю в файл."""
        with open(CASE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    def get_case_history(self, guild_id: int, user_id: int) -> list[dict]:
        """Получить историю открытий пользователя."""
        history = self._load_history()
        key = f"{guild_id}:{user_id}"
        return history.get(key, [])

    def enable_db(self) -> None:
        """Включить режим базы данных."""
        self._use_db = True


# Глобальный экземпляр
case_store = CaseStore()
