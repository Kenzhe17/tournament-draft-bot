"""Сервис для применения способностей питомцев."""

import logging
from typing import Any

from models.pet import Pet, AbilityType, ENERGY_COSTS
from storage.pet_store import pet_store

logger = logging.getLogger(__name__)


class PetAbilityService:
    """Сервис для расчёта и применения бонусов от питомцев."""

    async def get_pet_bonus(
        self,
        guild_id: int,
        user_id: int,
        action_type: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Получить бонус от питомца для действия.

        Args:
            guild_id: ID сервера
            user_id: ID пользователя
            action_type: Тип действия (case_open, pay, rob, etc.)
            context: Дополнительный контекст (например, базовая комиссия)

        Returns:
            Словарь с бонусами: {
                'has_pet': bool,
                'bonus_applied': bool,
                'bonus_value': float,
                'energy_deducted': int,
                'pet_name': str,
                'ability_name': str
            }
        """
        result = {
            "has_pet": False,
            "bonus_applied": False,
            "bonus_value": 0.0,
            "energy_deducted": 0,
            "pet_name": "",
            "ability_name": "",
        }

        # Получить питомца
        pet = await pet_store.get_user_pet(guild_id, user_id)
        if not pet:
            return result

        result["has_pet"] = True
        result["pet_name"] = pet.pet_name

        # Проверить энергию
        energy_cost = ENERGY_COSTS.get(action_type, 5)
        if pet.energy < energy_cost:
            return result

        # Получить определение питомца
        from storage.pet_store import pet_catalog
        pet_def = pet_catalog.get_pet(pet.pet_type.value)
        if not pet_def:
            return result

        # Получить соответствующую способность
        ability_data = self._get_ability_for_action(action_type, pet_def)
        if not ability_data:
            return result

        # Рассчитать бонус - теперь всегда максимальное значение
        max_val = ability_data.get("max_val", 0)
        bonus_value = max_val

        # Проверить энергию
        energy_cost = ENERGY_COSTS.get(action_type, 5)
        if pet.energy < energy_cost:
            return result

        # Списать энергию
        await pet_store.use_energy(pet.pet_id, energy_cost)
        result["energy_deducted"] = energy_cost

        # Добавить опыт за использование способности
        await pet_store.add_exp(pet.pet_id, 3)

        result["bonus_applied"] = True
        result["bonus_value"] = bonus_value
        result["ability_name"] = ability_data.get("name", "Способность")

        logger.info(
            f"Pet ability applied: user={user_id}, action={action_type}, "
            f"pet={pet.pet_name}, ability={ability.name}, bonus={bonus_value:.2f}"
        )

        return result

    def _get_ability_for_action(self, action_type: str, pet_def) -> dict | None:
        """Получить способность для типа действия."""
        # Сопоставление действий с названиями способностей
        action_to_ability_name = {
            "case_open": "Золотая лапа",
            "case_extra_reward": "Секретный тайник",
            "pay": "Щедрый перевод",
            "rob": "Ловкие лапы",
            "rob_loot": "Чужой кошелёк",
            "rob_defense": "Крепкий панцирь",
            "rob_item_protection": "Цепкие когти",
            "rob_loss_reduction": "Сохранность капитала",
            "sell": "Чёрный рынок",
            "elo_match": "Жажда победы",
            "elo_loss": "Несломленный дух",
        }

        ability_name = action_to_ability_name.get(action_type)
        if not ability_name:
            return None

        # Проверить способность 1
        if pet_def["abilities"][1]["name"] == ability_name:
            return pet_def["abilities"][1]

        # Проверить способность 2
        if pet_def["abilities"][2]["name"] == ability_name:
            return pet_def["abilities"][2]

        return None

    async def get_rob_defense_bonus(
        self,
        guild_id: int,
        victim_id: int,
    ) -> dict[str, Any]:
        """Получить бонус защиты при ограблении."""
        result = {
            "has_pet": False,
            "defense_bonus": 0.0,
            "item_protection_chance": 0.0,
            "loss_reduction": 0.0,
            "energy_deducted": 0,
        }

        pet = await pet_store.get_user_pet(guild_id, victim_id)
        if not pet:
            return result

        result["has_pet"] = True

        from storage.pet_store import pet_catalog
        pet_def = pet_catalog.get_pet(pet.pet_type.value)
        if not pet_def:
            return result

        effectiveness = 1.0  # Всегда полная эффективность
        energy_cost = ENERGY_COSTS.get("rob_defense", 9)

        # Проверить способности по названию
        for ability_num in [1, 2]:
            ability = pet_def["abilities"][ability_num]
            ability_name = ability["name"]
            max_val = ability.get("max_val", 0)
            ability_value = max_val  # Всегда максимальное значение

            if pet.energy >= energy_cost:
                if ability_name == "Крепкий панцирь":
                    result["defense_bonus"] = ability_value
                    await pet_store.use_energy(pet.pet_id, energy_cost)
                    result["energy_deducted"] = energy_cost
                    await pet_store.add_exp(pet.pet_id, 3)
                elif ability_name == "Цепкие когти":
                    result["item_protection_chance"] = ability_value
                    await pet_store.use_energy(pet.pet_id, energy_cost)
                    result["energy_deducted"] += energy_cost
                    await pet_store.add_exp(pet.pet_id, 3)
                elif ability_name in ["Сохранность капитала", "План отхода"]:
                    result["loss_reduction"] = ability_value
                    await pet_store.use_energy(pet.pet_id, energy_cost)
                    result["energy_deducted"] += energy_cost
                    await pet_store.add_exp(pet.pet_id, 3)

        return result

    async def is_item_protected(
        self,
        guild_id: int,
        user_id: int,
        item_id: str,
    ) -> bool:
        """Проверить защищён ли предмет (для Черепахи-хранителя)."""
        pet = await pet_store.get_user_pet(guild_id, user_id)
        if not pet:
            return False

        # Проверить совпадение ID предмета
        if pet.protected_item_id == item_id:
            from storage.pet_store import pet_catalog
            pet_def = pet_catalog.get_pet(pet.pet_type.value)
            if pet_def and pet_def["id"] == "guardian_turtle":
                # Способность открыта с 1 возраста
                return True

        return False

    async def apply_elo_bonus(
        self,
        guild_id: int,
        user_id: int,
        elo_change: int,
        is_win: bool,
    ) -> int:
        """Применить бонус к Elo изменению.

        Args:
            guild_id: ID сервера
            user_id: ID пользователя
            elo_change: Исходное изменение Elo
            is_win: Победа или поражение

        Returns:
            Модифицированное изменение Elo
        """
        # Способности повышающие ELO больше не применяются
        return elo_change


# Глобальный экземпляр
pet_ability_service = PetAbilityService()
