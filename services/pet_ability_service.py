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

        # Проверить сытость и энергию
        is_dead, pet = await pet_store.check_fullness_and_energy(pet.pet_id)
        if is_dead or not pet:
            return result

        # Получить определение питомца
        from storage.pet_store import pet_catalog
        pet_def = pet_catalog.get_pet(pet.pet_type.value)
        if not pet_def:
            return result

        # Получить соответствующую способность
        ability = self._get_ability_for_action(action_type, pet_def)
        if not ability:
            return result

        # Проверить разблокировку способности
        if not pet.can_use_ability(ability.unlock_level):
            return result

        # Проверить энергию
        energy_cost = ENERGY_COSTS.get(action_type, 5)
        if pet.energy < energy_cost:
            return result

        # Рассчитать бонус
        effectiveness = pet.get_ability_effectiveness()
        bonus_value = ability.max_value * effectiveness

        # Списать энергию
        await pet_store.use_energy(pet.pet_id, energy_cost)
        result["energy_deducted"] = energy_cost

        # Добавить опыт за использование способности
        await pet_store.add_exp(pet.pet_id, 3)

        result["bonus_applied"] = True
        result["bonus_value"] = bonus_value
        result["ability_name"] = ability.name

        logger.info(
            f"Pet ability applied: user={user_id}, action={action_type}, "
            f"pet={pet.pet_name}, ability={ability.name}, bonus={bonus_value:.2f}"
        )

        return result

    def _get_ability_for_action(self, action_type: str, pet_def) -> Any:
        """Получить способность для типа действия."""
        from models.pet import AbilityType

        action_to_ability = {
            "case_open": AbilityType.CASE_COIN_BONUS,
            "case_extra_reward": AbilityType.CASE_EXTRA_REWARD,
            "pay": AbilityType.PAY_FEE_REDUCTION,
            "rob": AbilityType.ROB_SUCCESS_BONUS,
            "rob_loot": AbilityType.ROB_LOOT_BONUS,
            "rob_defense": AbilityType.ROB_DEFENSE,
            "rob_item_protection": AbilityType.ROB_ITEM_PROTECTION,
            "rob_loss_reduction": AbilityType.ROB_LOSS_REDUCTION,
            "sell": AbilityType.SELL_FEE_REDUCTION,
            "elo_match": AbilityType.ELO_WIN_BONUS,
            "elo_loss": AbilityType.ELO_LOSS_REDUCTION,
        }

        ability_type = action_to_ability.get(action_type)
        if not ability_type:
            return None

        # Проверить способность 1
        if pet_def.ability_1.ability_type == ability_type:
            return pet_def.ability_1

        # Проверить способность 2
        if pet_def.ability_2.ability_type == ability_type:
            return pet_def.ability_2

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

        is_dead, pet = await pet_store.check_fullness_and_energy(pet.pet_id)
        if is_dead or not pet:
            return result

        from storage.pet_store import pet_catalog
        pet_def = pet_catalog.get_pet(pet.pet_type.value)
        if not pet_def:
            return result

        effectiveness = pet.get_ability_effectiveness()
        energy_cost = ENERGY_COSTS.get("rob_defense", 9)

        # Проверить способность защиты (Крепкий панцирь)
        if pet.can_use_ability(pet_def.ability_1.unlock_level):
            if pet_def.ability_1.ability_type == AbilityType.ROB_DEFENSE:
                if pet.energy >= energy_cost:
                    result["defense_bonus"] = pet_def.ability_1.max_value * effectiveness
                    await pet_store.use_energy(pet.pet_id, energy_cost)
                    result["energy_deducted"] = energy_cost
                    await pet_store.add_exp(pet.pet_id, 3)

        # Проверить защиту от кражи предметов (Цепкие когти)
        if pet.can_use_ability(pet_def.ability_2.unlock_level):
            if pet_def.ability_2.ability_type == AbilityType.ROB_ITEM_PROTECTION:
                if pet.energy >= energy_cost:
                    result["item_protection_chance"] = pet_def.ability_2.max_value * effectiveness
                    await pet_store.use_energy(pet.pet_id, energy_cost)
                    result["energy_deducted"] += energy_cost
                    await pet_store.add_exp(pet.pet_id, 3)

        # Проверить снижение потерь (Сохранность капитала / План отхода)
        for ability in [pet_def.ability_1, pet_def.ability_2]:
            if ability.ability_type == AbilityType.ROB_LOSS_REDUCTION:
                if pet.can_use_ability(ability.unlock_level):
                    if pet.energy >= energy_cost:
                        result["loss_reduction"] = ability.max_value * effectiveness
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

        is_dead, pet = await pet_store.check_fullness_and_energy(pet.pet_id)
        if is_dead or not pet:
            return False

        # Проверить совпадение ID предмета
        if pet.protected_item_id == item_id:
            from storage.pet_store import pet_catalog
            pet_def = pet_catalog.get_pet(pet.pet_type.value)
            if pet_def and pet_def.pet_type.value == "guardian_turtle":
                if pet.can_use_ability(2):  # Способность 2 на уровне 7+
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
        pet = await pet_store.get_user_pet(guild_id, user_id)
        if not pet:
            return elo_change

        is_dead, pet = await pet_store.check_fullness_and_energy(pet.pet_id)
        if is_dead or not pet:
            return elo_change

        from storage.pet_store import pet_catalog
        pet_def = pet_catalog.get_pet(pet.pet_type.value)
        if not pet_def:
            return elo_change

        effectiveness = pet.get_ability_effectiveness()
        energy_cost = ENERGY_COSTS.get("elo_match", 15)

        if pet.energy < energy_cost:
            return elo_change

        # Проверить способности Волка арены
        if pet_def.pet_type.value == "arena_wolf":
            if is_win:
                # Жажда победы
                if pet.can_use_ability(pet_def.ability_1.unlock_level):
                    bonus = int(elo_change * (pet_def.ability_1.max_value / 100 * effectiveness))
                    await pet_store.use_energy(pet.pet_id, energy_cost)
                    await pet_store.add_exp(pet.pet_id, 5)
                    return elo_change + bonus
            else:
                # Несломленный дух
                if pet.can_use_ability(pet_def.ability_2.unlock_level):
                    reduction = int(abs(elo_change) * (pet_def.ability_2.max_value / 100 * effectiveness))
                    await pet_store.use_energy(pet.pet_id, energy_cost)
                    await pet_store.add_exp(pet.pet_id, 5)
                    # Снизить потерю, но не превратить в положительную
                    new_change = -max(0, abs(elo_change) - reduction)
                    return new_change

        return elo_change


# Глобальный экземпляр
pet_ability_service = PetAbilityService()
