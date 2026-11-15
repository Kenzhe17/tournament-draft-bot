"""Storage для системы кейсов."""

import json
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any

from models.case import Case
from models.shop_item import CosmeticRarity
from storage.shop_store import shop_store
from storage.user_balance_store import user_balance_store
from storage.shop_store import inventory_store

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass

from config import DATA_DIR  # noqa: E402 — единый путь из config
from storage.json_atomic import atomic_write_json
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
            
            # Обновить названия кейсов - убрать стандартные эмодзи
            self._update_case_names()
        except (json.JSONDecodeError, KeyError):
            self._initialize_default_cases()
    
    def _update_case_names(self) -> None:
        """Обновить названия кейсов - убрать стандартные эмодзи."""
        name_mapping = {
            "📦 Basic Case": "Basic",
            "🎁 Premium Case": "Premium",
            "💎 Elite Case": "Elite",
            "✨ Special Case": "Special",
        }
        updated = False
        for case_id, case in self._cases.items():
            if case.name in name_mapping:
                case.name = name_mapping[case.name]
                updated = True
        if updated:
            self.save()

    def _update_drop_rates_if_needed(self) -> None:
        """Миграция старых кейсов: только ДОБАВЛЯЕМ недостающие ключи.

        Ранее метод перезаписывал drop_rates всякий раз, когда
        `nothing` отличался от 0.50 — это молча уничтожало
        админские настройки вероятностей. Теперь кастомные
        коэффициенты сохраняются; корректируем только кейсы,
        у которых вообще нет распознаваемых ключей.
        """
        updated = False
        for case in self._cases.values():
            keys = set(case.drop_rates.keys())
            has_structure = any(
                k.startswith(("item_", "coins_", "nothing")) for k in keys
            )
            if has_structure:
                continue
            # Пустой/нераспознанный формат — применяем стандарт
            case.drop_rates = {
                "item_basic": 0.10,
                "coins_100": 0.12,
                "coins_200": 0.10,
                "coins_300": 0.08,
                "coins_400": 0.06,
                "coins_500": 0.04,
                "nothing": 0.50,
            }
            updated = True
        if updated:
            self.save()

    def save(self) -> None:
        """Сохранить кейсы в файл."""
        data = {
            case_id: case.to_dict()
            for case_id, case in self._cases.items()
        }
        atomic_write_json(CASES_FILE, data)

    def _initialize_default_cases(self) -> None:
        """Инициализировать кейсы по умолчанию."""
        self._cases = {
            "basic": Case(
                id="basic",
                name="Basic",
                description="Базовые предметы",
                price=200,
                drop_rates={
                    "item_basic": 0.10,
                    "coins_100": 0.12,
                    "coins_200": 0.10,
                    "coins_300": 0.08,
                    "coins_400": 0.06,
                    "coins_500": 0.04,
                    "nothing": 0.50,
                },
                is_active=True
            ),
            "premium": Case(
                id="premium",
                name="Premium",
                description="Редкие предметы",
                price=500,
                drop_rates={
                    "item_premium": 0.10,
                    "coins_250": 0.12,
                    "coins_500": 0.10,
                    "coins_750": 0.08,
                    "coins_1000": 0.06,
                    "coins_1250": 0.04,
                    "nothing": 0.50,
                },
                is_active=True
            ),
            "elite": Case(
                id="elite",
                name="Elite",
                description="Легендарные предметы",
                price=1000,
                drop_rates={
                    "item_elite": 0.10,
                    "coins_500": 0.12,
                    "coins_1000": 0.10,
                    "coins_1500": 0.08,
                    "coins_2000": 0.06,
                    "coins_2500": 0.04,
                    "nothing": 0.50,
                },
                is_active=True
            ),
            "special": Case(
                id="special",
                name="Special",
                description="Эксклюзивные предметы",
                price=2000,
                drop_rates={
                    "item_special": 0.10,
                    "coins_1000": 0.12,
                    "coins_2000": 0.10,
                    "coins_3000": 0.08,
                    "coins_4000": 0.06,
                    "coins_5000": 0.04,
                    "nothing": 0.50,
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

    async def open_case(self, guild_id: int, user_id: int, case_id: str, guild, precharged: bool = False,
                        grant_key: str | None = None,
                        on_pending_reward=None) -> dict[str, Any]:
        """Открыть кейс.

        Args:
            guild_id: ID сервера
            user_id: ID пользователя
            case_id: ID кейса
            guild: Discord guild object
            precharged: True — списание уже выполнено durable-операцией
                shop_operations (списание и статус charged атомарны);
                проверка баланса и subtract_balance пропускаются.
            grant_key: Ключ идемпотентного гранта монет
                (add_balance_once) для durable-режима. None — обычное
                add_balance (legacy).
            on_pending_reward: async callback(desc) — вызывается
                СРАЗУ после выбора конкретной награды, ДО её выдачи.
                Durable-режим пишет desc в shop_operations.payload
                (case_reward): recovery может довести выдачу до конца
                или вернуть деньги, но не оба сразу.

        Returns:
            Словарь с результатом: {"type": "item"/"coins"/"nothing", "value": ..., "rarity": ...}
        """
        case = self.get_case(case_id)
        if not case:
            return {"type": "error", "value": "Кейс не найден"}

        if not precharged:
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
        pet_bonus_info = {"golden_fox_bonus": 0, "golden_fox_name": None, "mimic_bonus": 0, "mimic_name": None}

        async def _persist_reward(desc: dict) -> None:
            """Зафиксировать выбранную награду ДО её выдачи."""
            if on_pending_reward is not None:
                try:
                    await on_pending_reward(desc)
                except Exception as exc:
                    logger.error(f"case reward persist failed: {exc}")
                    raise

        async def _grant_coins(amount: int, key_suffix: str = "") -> None:
            if grant_key:
                await user_balance_store.add_balance_once(
                    guild_id, user_id, amount,
                    f"{grant_key}{key_suffix}",
                )
            else:
                await user_balance_store.add_balance(guild_id, user_id, amount)

        if drop_type == "nothing":
            await _persist_reward({"kind": "nothing"})
            result = {"type": "nothing", "value": "Ничего", "rarity": "common"}
        elif drop_type.startswith("coins_"):
            coins_amount = int(drop_type.split("_")[1])

            # Применить бонус питомца к монетам (Золотая лапа)
            try:
                from storage.pet_store import pet_store
                pet = await pet_store.get_user_pet(guild_id, user_id)

                if pet:
                    # Списать энергию за использование способности
                    energy_consumed = await pet_store.consume_energy_for_ability(guild_id, user_id, "case_open")

                    if energy_consumed:
                        from storage.pet_store import pet_catalog
                        pet_def = pet_catalog.get_pet(pet.pet_type.value)

                        if pet_def:  # Способности открыты с 1 возраста
                            ability = pet_def["abilities"][1]
                            if ability["name"] == "Золотая лапа":
                                bonus_percent = ability.get("max_val", 0)
                                bonus_coins = int(coins_amount * (bonus_percent / 100))
                                coins_amount += bonus_coins
                                pet_bonus_info["golden_fox_bonus"] = bonus_percent
                                pet_bonus_info["golden_fox_name"] = pet.pet_name
            except Exception as e:
                logger.error(f"Error applying pet bonus to case: {e}")

            # Применить Секретный тайник (Мимик-собиратель) - только к монетам
            # Работает только если энергии достаточно
            try:
                from storage.pet_store import pet_store
                pet = await pet_store.get_user_pet(guild_id, user_id)

                if pet and pet.energy >= 30:
                    from storage.pet_store import pet_catalog
                    pet_def = pet_catalog.get_pet(pet.pet_type.value)

                    if pet_def and pet_def["id"] == "mimic_collector":
                        ability_mimic = pet_def["abilities"][1]
                        if ability_mimic["name"] == "Секретный тайник":
                            mimic_chance = ability_mimic.get("max_val", 0)
                            if random.random() < (mimic_chance / 100):
                                bonus_range = random.uniform(0.25, 0.5)
                                extra_coins = int(coins_amount * bonus_range)
                                coins_amount += extra_coins
                                pet_bonus_info["mimic_bonus"] = extra_coins
                                pet_bonus_info["mimic_name"] = pet.pet_name
            except Exception as e:
                logger.error(f"Error applying mimic bonus to case: {e}")

            await _persist_reward({"kind": "coins", "amount": coins_amount})
            await _grant_coins(coins_amount)
            result = {"type": "coins", "value": coins_amount, "rarity": "common", "pet_bonus": pet_bonus_info}
        elif drop_type.startswith("item_"):
            # Выбрать случайный предмет по редкости
            rarity_str = drop_type.replace("item_", "")
            rarity_map = {
                "basic": CosmeticRarity.BASIC,
                "premium": CosmeticRarity.PREMIUM,
                "elite": CosmeticRarity.ELITE,
                "special": CosmeticRarity.SPECIAL,
            }
            rarity = rarity_map.get(rarity_str)
            if rarity:
                # Ensure shop items are loaded
                if not shop_store._items:
                    from storage.shop_items import initialize_shop_items
                    initialize_shop_items()

                items = shop_store.get_items_by_rarity(rarity)
                # Fallback to coins if no items of this rarity
                if not items:
                    fallback_coins = case.price // 2
                    await _persist_reward({"kind": "coins", "amount": fallback_coins, "key_suffix": ":fb"})
                    await _grant_coins(fallback_coins, ":fb")
                    result = {"type": "coins", "value": fallback_coins, "rarity": "common", "pet_bonus": pet_bonus_info}
                else:
                    item = random.choice(items)
                    # Проверить есть ли уже этот предмет в инвентаре
                    player_inventory = inventory_store.get_player_inventory(guild_id, user_id)
                    has_item = any(cosmetic.item_id == item.id for cosmetic in player_inventory)

                    if has_item:
                        # Предмет уже есть - выдать полную стоимость
                        await _persist_reward({"kind": "coins", "amount": item.price, "key_suffix": ":dup"})
                        await _grant_coins(item.price, ":dup")
                        result = {"type": "coins", "value": item.price, "rarity": "common", "pet_bonus": pet_bonus_info}
                    else:
                        # Добавить в инвентарь
                        await _persist_reward({"kind": "item", "item_id": item.id})
                        from models.shop_item import PlayerCosmetic
                        cosmetic = PlayerCosmetic(
                            guild_id=guild_id,
                            user_id=user_id,
                            item_id=item.id,
                            equipped=False
                        )
                        inventory_store.add_cosmetic(cosmetic)
                        result = {"type": "item", "value": item.name, "rarity": item.rarity.value, "item_id": item.id, "pet_bonus": pet_bonus_info}
            else:
                result = {"type": "nothing", "value": "Ничего", "rarity": "common", "pet_bonus": pet_bonus_info}
        else:
            await _persist_reward({"kind": "nothing"})
            result = {"type": "nothing", "value": "Ничего", "rarity": "common", "pet_bonus": pet_bonus_info}

        # Записать в историю
        self._add_to_history(guild_id, user_id, case_id, result)

        # Проверить дополнительную награду от питомца (Секретный тайник)
        try:
            from storage.pet_store import pet_store
            pet = await pet_store.get_user_pet(guild_id, user_id)
            
            if pet and pet.fullness > 0:
                from storage.pet_store import pet_catalog
                pet_def = pet_catalog.get_pet(pet.pet_type.value)
                
                if pet_def:  # Способности открыты с 1 возраста
                    ability = pet_def["abilities"][1]
                    if ability["name"] == "Секретный тайник":
                        bonus_chance = ability.get("max_val", 0)

                        if random.random() < (bonus_chance / 100):
                            # Дополнительная награда: 25-50% от базовой
                            if result["type"] == "coins":
                                base_value = result["value"]
                                extra_percent = random.uniform(0.25, 0.50)
                                extra_value = int(base_value * extra_percent)
                                await user_balance_store.add_balance(guild_id, user_id, extra_value)
                                result["extra_reward"] = extra_value
        except Exception as e:
            logger.error(f"Error applying pet extra reward: {e}")

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
        atomic_write_json(CASE_HISTORY_FILE, history)

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
