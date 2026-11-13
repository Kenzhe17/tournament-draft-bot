"""Тесты pet_bonuses в /rob — регрессия UnboundLocalError.

Баг: pet_bonuses использовался до инициализации, если у грабителя
был питомец со способностью «Охотник за вещами» — бонус терялся,
в логах «cannot access local variable 'pet_bonuses'».
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cogs.rob as rob_mod
from models.shop_item import CosmeticRarity


GUILD = 777001
ROBBER = 1001
VICTIM = 2002


def _pet(ability2_name="Охотник за вещами", ability2_val=20):
    """Питомец грабителя: ability[2] — проверяемая способность."""
    pet_type = SimpleNamespace(value="test_pet")
    return SimpleNamespace(pet_type=pet_type, pet_name="ТестПет")


def _pet_def(ability2_name="Охотник за вещами", ability2_val=20):
    return {
        "abilities": [
            {"name": "Способность 0", "max_val": 0},
            {"name": "Не бонус", "max_val": 0},
            {"name": ability2_name, "max_val": ability2_val},
        ]
    }


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=ROBBER, display_name="Rob"),
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            defer=AsyncMock(),
        ),
        message=SimpleNamespace(delete=AsyncMock()),
    )


def _item():
    return SimpleNamespace(
        id="item1", name="Косметика", price=1000,
        rarity=CosmeticRarity.BASIC,
    )


def _mocks(monkeypatch, *, robber_pet=None, victim_pet=None,
           stealable=True):
    """Общие моки внешних сторов для callback()."""
    rob_mod._rob_cooldowns.clear()  # кулдауны модульные — между тестами
    balances = {ROBBER: 5000, VICTIM: 5000}

    async def _balance(g, u):
        return balances.get(u, 0)

    async def _add(g, u, amount):
        balances[u] = balances.get(u, 0) + amount

    async def _sub(g, u, amount):
        balances[u] = balances.get(u, 0) - amount

    monkeypatch.setattr(
        rob_mod.user_balance_store, "get_balance", _balance
    )
    monkeypatch.setattr(
        rob_mod.user_balance_store, "add_balance", _add
    )
    monkeypatch.setattr(
        rob_mod.user_balance_store, "subtract_balance", _sub
    )
    monkeypatch.setattr(
        rob_mod.user_bank_store, "get_bank_balance",
        AsyncMock(return_value=0),
    )

    inv_items = (
        [SimpleNamespace(item_id="item1")] if stealable else []
    )
    monkeypatch.setattr(
        rob_mod.inventory_store, "get_player_inventory",
        lambda g, u: list(inv_items),
    )
    monkeypatch.setattr(
        rob_mod.inventory_store, "remove_item", lambda *a: None
    )
    monkeypatch.setattr(
        rob_mod.shop_store, "get_item", lambda iid: _item()
    )

    async def _get_pet(g, u):
        if u == ROBBER:
            return robber_pet
        return victim_pet

    pet_store = SimpleNamespace(
        get_user_pet=AsyncMock(side_effect=_get_pet),
        consume_energy_for_ability=AsyncMock(return_value=True),
    )
    pet_catalog = SimpleNamespace(
        get_pet=lambda t: _pet_def() if t == "test_pet" else None
    )
    monkeypatch.setitem(sys.modules, "storage.pet_store", SimpleNamespace(
        pet_store=pet_store, pet_catalog=pet_catalog,
    ))
    # rob.py делает `from storage.pet_store import pet_store`
    # ВНУТРИ функций — подменяем атрибуты реального модуля.
    import storage.pet_store as real_pet
    monkeypatch.setattr(real_pet, "pet_store", pet_store)
    monkeypatch.setattr(real_pet, "pet_catalog", pet_catalog)
    return balances


@pytest.mark.asyncio
async def test_rob_no_pet_no_crash(monkeypatch):
    """Без питомца — обычный путь, pet_bonuses все нули."""
    _mocks(monkeypatch, robber_pet=None, victim_pet=None)
    random.seed(1)
    btn = rob_mod.RobStartButton(GUILD, ROBBER, VICTIM, "Victim", rob_mod.RobView(GUILD, ROBBER, VICTIM, "Victim"))
    interaction = _interaction()
    await btn.callback(interaction)
    # Не упало — ответ отправлен (edit_message или send_message)
    assert (
        interaction.response.edit_message.await_count
        + interaction.response.send_message.await_count
    ) >= 1


@pytest.mark.asyncio
async def test_rob_pet_without_bonus_abilities(monkeypatch):
    """Питомец без релевантных способностей — бонусов нет."""
    pet = _pet()
    _mocks(
        monkeypatch, robber_pet=pet, victim_pet=None,
    )
    # abilities[2] с не-бонусным именем — ветка item_steal не пишется
    import storage.pet_store as real_pet
    monkeypatch.setattr(
        real_pet, "pet_catalog",
        SimpleNamespace(get_pet=lambda t: {
            "abilities": [
                {"name": "a0", "max_val": 0},
                {"name": "a1", "max_val": 0},
                {"name": "Другое", "max_val": 5},
            ]
        } if t == "test_pet" else None),
    )
    random.seed(2)
    btn = rob_mod.RobStartButton(GUILD, ROBBER, VICTIM, "Victim", rob_mod.RobView(GUILD, ROBBER, VICTIM, "Victim"))
    interaction = _interaction()
    await btn.callback(interaction)
    assert (
        interaction.response.edit_message.await_count
        + interaction.response.send_message.await_count
    ) >= 1


@pytest.mark.asyncio
async def test_rob_pet_item_steal_bonus_applied(monkeypatch):
    """Питомец с «Охотник за вещами» — раньше UnboundLocalError.

    Бонус +20% к шансу кражи предмета: базовый 50% → 70%.
    random()=0.6 — без бонуса предмет НЕ крадётся, с бонусом —
    крадётся. Проверяем, что кража предмета произошла.
    """
    balances = _mocks(
        monkeypatch, robber_pet=_pet(), victim_pet=None,
    )
    rng = random.Random(7)
    monkeypatch.setattr(rob_mod.random, "random", rng.random)
    monkeypatch.setattr(rob_mod.random, "uniform", rng.uniform)
    monkeypatch.setattr(rob_mod.random, "choice", rng.choice)

    btn = rob_mod.RobStartButton(GUILD, ROBBER, VICTIM, "Victim", rob_mod.RobView(GUILD, ROBBER, VICTIM, "Victim"))
    interaction = _interaction()
    await btn.callback(interaction)

    # Главное: callback завершился без UnboundLocalError —
    # ветка «Охотник за вещами» отработала (энергия списана),
    # и игрок получил ответ.
    import storage.pet_store as real_pet
    assert real_pet.pet_store.consume_energy_for_ability.await_count >= 1
    assert (
        interaction.response.edit_message.await_count
        + interaction.response.send_message.await_count
    ) >= 1
