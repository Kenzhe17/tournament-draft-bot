"""Сервис для расчёта всех пассивных бонусов питомцев."""

import logging
from typing import Dict, Any

from models.pets_catalog import PETS_CATALOG
from storage.db import get_pool

logger = logging.getLogger(__name__)


def calculate_ability_power(level: int, min_val: float, max_val: float, unlock_level: int = 3) -> float:
    """
    Рассчитывает силу способности в зависимости от уровня питомца.
    
    Формула: Power = min_val + (level - unlock_level) * (max_val - min_val) / (10 - unlock_level)
    Ограничивается диапазоном [min_val, max_val].
    """
    if level < unlock_level:
        return 0.0
    
    # Если уровень >= 10, используем максимальное значение
    if level >= 10:
        return max_val
    
    power = min_val + (level - unlock_level) * (max_val - min_val) / (10 - unlock_level)
    return max(min_val, min(max_val, power))


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
        if level >= abilities[1]["unlock_level"]:
            bonuses["case_coin_bonus_pct"] = calculate_ability_power(
                level, abilities[1]["min_val"], abilities[1]["max_val"], abilities[1]["unlock_level"]
            )
        if level >= abilities[2]["unlock_level"]:
            bonuses["pay_fee_reduction_pct"] = calculate_ability_power(
                level, abilities[2]["min_val"], abilities[2]["max_val"], abilities[2]["unlock_level"]
            )

    # --- Лис-разведчик ---
    elif pet_type == "scout_fox":
        if level >= abilities[1]["unlock_level"]:
            bonuses["rob_success_rate_add"] = calculate_ability_power(
                level, abilities[1]["min_val"], abilities[1]["max_val"], abilities[1]["unlock_level"]
            )
        if level >= abilities[2]["unlock_level"]:
            bonuses["jail_time_reduction_pct"] = calculate_ability_power(
                level, abilities[2]["min_val"], abilities[2]["max_val"], abilities[2]["unlock_level"]
            )

    # --- Сова-казначей ---
    elif pet_type == "treasury_owl":
        if level >= abilities[1]["unlock_level"]:
            bonuses["rob_loss_reduction_pct"] = calculate_ability_power(
                level, abilities[1]["min_val"], abilities[1]["max_val"], abilities[1]["unlock_level"]
            )
        if level >= abilities[2]["unlock_level"]:
            bonuses["item_protect_chance"] = calculate_ability_power(
                level, abilities[2]["min_val"], abilities[2]["max_val"], abilities[2]["unlock_level"]
            )

    # --- Енот-грабитель ---
    elif pet_type == "raccoon_robber":
        if level >= abilities[1]["unlock_level"]:
            bonuses["rob_loot_bonus_pct"] = calculate_ability_power(
                level, abilities[1]["min_val"], abilities[1]["max_val"], abilities[1]["unlock_level"]
            )
        if level >= abilities[2]["unlock_level"]:
            bonuses["blackmarket_fee_red_pct"] = calculate_ability_power(
                level, abilities[2]["min_val"], abilities[2]["max_val"], abilities[2]["unlock_level"]
            )

    # --- Ворон-контрабандист ---
    elif pet_type == "raven_smuggler":
        if level >= abilities[1]["unlock_level"]:
            bonuses["rob_fail_penalty_red_pct"] = calculate_ability_power(
                level, abilities[1]["min_val"], abilities[1]["max_val"], abilities[1]["unlock_level"]
            )
        if level >= abilities[2]["unlock_level"]:
            bonuses["item_steal_rate_add"] = calculate_ability_power(
                level, abilities[2]["min_val"], abilities[2]["max_val"], abilities[2]["unlock_level"]
            )

    # --- Мимик-собиратель ---
    elif pet_type == "mimic_collector":
        if level >= abilities[1]["unlock_level"]:
            bonuses["mimic_extra_reward_chance"] = calculate_ability_power(
                level, abilities[1]["min_val"], abilities[1]["max_val"], abilities[1]["unlock_level"]
            )
        if level >= abilities[2]["unlock_level"]:
            bonuses["trade_fee_red_pct"] = calculate_ability_power(
                level, abilities[2]["min_val"], abilities[2]["max_val"], abilities[2]["unlock_level"]
            )

    # --- Черепаха-хранитель ---
    elif pet_type == "guardian_turtle":
        if level >= abilities[1]["unlock_level"]:
            bonuses["defend_rob_rate_red"] = calculate_ability_power(
                level, abilities[1]["min_val"], abilities[1]["max_val"], abilities[1]["unlock_level"]
            )
        if level >= abilities[2]["unlock_level"]:
            bonuses["has_absolute_item_shield"] = True

    # --- Волк арены ---
    elif pet_type == "arena_wolf":
        if level >= abilities[1]["unlock_level"]:
            bonuses["elo_gain_bonus_pct"] = calculate_ability_power(
                level, abilities[1]["min_val"], abilities[1]["max_val"], abilities[1]["unlock_level"]
            )
        if level >= abilities[2]["unlock_level"]:
            bonuses["elo_loss_reduction_pct"] = calculate_ability_power(
                level, abilities[2]["min_val"], abilities[2]["max_val"], abilities[2]["unlock_level"]
            )

    return bonuses
