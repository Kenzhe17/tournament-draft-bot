"""Сервис для расчёта всех пассивных бонусов питомцев."""

import logging
from typing import Dict, Any

from models.pets_catalog import PETS_CATALOG
from storage.db import get_pool

logger = logging.getLogger(__name__)


def calculate_ability_power(level: int, max_val: float, unlock_level: int = 1) -> float:
    """
    Рассчитывает силу способности - теперь всегда максимальное значение.
    
    Способности открыты с 1 возраста и работают на полную мощность.
    """
    return max_val


async def get_user_active_pet_bonuses(user_id: int) -> Dict[str, Any]:
    """
    Возвращает полный словарь всех возможных бонусов игрока.
    Если питомца нет или его сытость == 0, все бонусы возвращаются с нулевыми значениями.
    """
    bonuses = {
        # Золотой лис
        "case_coin_bonus_pct": 0.0,
        "pay_fee_reduction_pct": 0.0,
        # Лис-разведчик
        "rob_success_rate_add": 0.0,
        "jail_time_reduction_pct": 0.0,
        # Сова-казначей
        "rob_loss_reduction_pct": 0.0,
        "item_protect_chance": 0.0,
        # Енот-грабитель
        "rob_loot_bonus_pct": 0.0,
        "blackmarket_fee_red_pct": 0.0,
        # Ворон-контрабандист
        "rob_fail_penalty_red_pct": 0.0,
        "item_steal_rate_add": 0.0,
        # Мимик-собиратель
        "mimic_extra_reward_chance": 0.0,
        "trade_fee_red_pct": 0.0,
        # Черепаха-хранитель
        "defend_rob_rate_red": 0.0,
        "has_absolute_item_shield": False,
        # Волк арены
        "elo_gain_bonus_pct": 0.0,
        "elo_loss_reduction_pct": 0.0,
        # Служебные данные
        "pet_name": None,
        "pet_type": None,
        "level": 0,
        "has_pet": False
    }

    pool = await get_pool()
    async with pool.acquire() as conn:
        pet = await conn.fetchrow(
            "SELECT * FROM pets WHERE owner_1_id = $1 OR owner_2_id = $1",
            user_id
        )

    # Если питомца нет или он полностью голоден (0% сытости)
    if not pet or pet["fullness"] <= 0:
        return bonuses

    pet_type = pet["pet_type"]
    level = pet["level"]
    catalog = PETS_CATALOG.get(pet_type)
    if not catalog:
        return bonuses

    bonuses["pet_name"] = pet["pet_name"]
    bonuses["pet_type"] = pet_type
    bonuses["level"] = level
    bonuses["has_pet"] = True
    abilities = catalog["abilities"]

    # --- Золотой лис ---
    if pet_type == "golden_fox":
        bonuses["case_coin_bonus_pct"] = calculate_ability_power(
            level, abilities[1]["max_val"], abilities[1].get("unlock_level", 1)
        )
        bonuses["pay_fee_reduction_pct"] = calculate_ability_power(
            level, abilities[2]["max_val"], abilities[2].get("unlock_level", 1)
        )

    # --- Лис-разведчик ---
    elif pet_type == "scout_fox":
        bonuses["rob_success_rate_add"] = calculate_ability_power(
            level, abilities[1]["max_val"], abilities[1].get("unlock_level", 1)
        )
        bonuses["jail_time_reduction_pct"] = calculate_ability_power(
            level, abilities[2]["max_val"], abilities[2].get("unlock_level", 1)
        )

    # --- Сова-казначей ---
    elif pet_type == "treasury_owl":
        bonuses["rob_loss_reduction_pct"] = calculate_ability_power(
            level, abilities[1]["max_val"], abilities[1].get("unlock_level", 1)
        )
        bonuses["item_protect_chance"] = calculate_ability_power(
            level, abilities[2]["max_val"], abilities[2].get("unlock_level", 1)
        )

    # --- Енот-грабитель ---
    elif pet_type == "raccoon_robber":
        bonuses["rob_loot_bonus_pct"] = calculate_ability_power(
            level, abilities[1]["max_val"], abilities[1].get("unlock_level", 1)
        )
        bonuses["blackmarket_fee_red_pct"] = calculate_ability_power(
            level, abilities[2]["max_val"], abilities[2].get("unlock_level", 1)
        )

    # --- Ворон-контрабандист ---
    elif pet_type == "raven_smuggler":
        bonuses["rob_fail_penalty_red_pct"] = calculate_ability_power(
            level, abilities[1]["max_val"], abilities[1].get("unlock_level", 1)
        )
        bonuses["item_steal_rate_add"] = calculate_ability_power(
            level, abilities[2]["max_val"], abilities[2].get("unlock_level", 1)
        )

    # --- Мимик-собиратель ---
    elif pet_type == "mimic_collector":
        bonuses["mimic_extra_reward_chance"] = calculate_ability_power(
            level, abilities[1]["max_val"], abilities[1].get("unlock_level", 1)
        )
        bonuses["trade_fee_red_pct"] = calculate_ability_power(
            level, abilities[2]["max_val"], abilities[2].get("unlock_level", 1)
        )

    # --- Черепаха-хранитель ---
    elif pet_type == "guardian_turtle":
        bonuses["defend_rob_rate_red"] = calculate_ability_power(
            level, abilities[1]["max_val"], abilities[1].get("unlock_level", 1)
        )
        bonuses["has_absolute_item_shield"] = True

    return bonuses
