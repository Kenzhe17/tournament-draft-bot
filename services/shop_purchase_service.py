"""Единый сервис покупки товаров магазина (SHOP 2.0).

Централизует: поиск товара → проверки → атомарное списание →
выдача → журнал. Per-user asyncio.Lock сериализует покупки
одного пользователя (двойной клик/повторная отправка не может
списать дважды: проверка владения повторяется внутри блокировки,
а subtract_balance атомарен на уровне PG).

Для Discord-действий (роли) результат не входит в PG-транзакцию:
при подтверждённой ошибке выдачи — один идемпотентный возврат
через add_balance_once с ключом shopref:{op_id}.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from models.shop_item import PlayerCosmetic, ShopItem
from storage.shop_ops_store import shop_ops_store
from storage.shop_store import inventory_store, shop_store
from storage.user_balance_store import user_balance_store

if TYPE_CHECKING:
    pass


@dataclass
class PurchaseResult:
    ok: bool
    message: str
    item: ShopItem | None = None
    granted: list[str] = field(default_factory=list)  # выданные item_id
    refunded: bool = False
    insufficient: bool = False


# Per-user locks: "{guild}:{user}" -> asyncio.Lock
_user_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _user_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    key = f"{guild_id}:{user_id}"
    async with _locks_guard:
        lock = _user_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[key] = lock
        return lock


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def item_available_now(item: ShopItem) -> bool:
    """Активен ли товар сейчас (is_active + сезонное окно)."""
    if not item.is_active:
        return False
    now = datetime.now(timezone.utc)
    start = _parse_dt(item.available_from)
    end = _parse_dt(item.available_until)
    # naive ISO трактуем как UTC
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start is not None and now < start:
        return False
    if end is not None and now > end:
        return False
    return True


def owned_item_ids(guild_id: int, user_id: int) -> set[str]:
    return {
        c.item_id
        for c in inventory_store.get_player_inventory(guild_id, user_id)
    }


async def _check_level(guild_id: int, user_id: int, item: ShopItem) -> bool:
    if item.required_level <= 0:
        return True
    from storage.player_stats_store import player_stats_store
    stats = await player_stats_store.get(guild_id, user_id)
    return bool(stats and stats.level >= item.required_level)


async def _grant_item(
    guild_id: int,
    user_id: int,
    item: ShopItem,
    guild: discord.Guild | None,
) -> tuple[bool, str]:
    """Выдать один предмет. Возвращает (успех, деталь ошибки)."""
    # Цветные роли не выдаются при покупке — только владение;
    # Discord-роль назначается отдельной активацией.
    if item.item_type == "role" and item.category == "color_roles":
        ok = inventory_store.add_cosmetic(PlayerCosmetic(
            guild_id=guild_id, user_id=user_id,
            item_id=item.id, equipped=False,
        ))
        return ok, "" if ok else "уже есть"

    if item.item_type == "role":
        if not item.role_id or guild is None:
            return False, "нет role_id/guild"
        from utils.role_manager import apply_role
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None
        success = await apply_role(guild, user_id, item.role_id)
        if not success:
            return False, "не удалось назначить роль"
        inventory_store.add_cosmetic(PlayerCosmetic(
            guild_id=guild_id, user_id=user_id,
            item_id=item.id, equipped=True,
        ))
        return True, ""

    ok = inventory_store.add_cosmetic(PlayerCosmetic(
        guild_id=guild_id, user_id=user_id,
        item_id=item.id, equipped=False,
    ))
    return ok, "" if ok else "уже есть"


async def _refund_once(
    guild_id: int, user_id: int, amount: int, op_id: str
) -> None:
    if amount <= 0:
        return
    try:
        await user_balance_store.add_balance_once(
            guild_id, user_id, amount, f"shopref:{op_id}"
        )
    except Exception:
        # JSON-fallback add_balance_once не идемпотентен —
        # но вызывается один раз на op_id, приемлемо.
        try:
            await user_balance_store.add_balance(guild_id, user_id, amount)
        except Exception:
            pass


async def purchase(
    guild_id: int,
    user_id: int,
    item_id: str,
    guild: discord.Guild | None = None,
) -> PurchaseResult:
    """Купить товар (или набор). Сервисные товары отклоняются —
    у них собственный сценарий (nickname_service)."""
    async with await _user_lock(guild_id, user_id):
        item = shop_store.get_item(item_id)
        if item is None:
            return PurchaseResult(False, "Товар не найден.")
        if item.item_type == "service":
            return PurchaseResult(
                False, "Услуга покупается через свой сценарий.", item=item
            )
        if not item_available_now(item):
            return PurchaseResult(False, "Товар недоступен.", item=item)
        if not await _check_level(guild_id, user_id, item):
            return PurchaseResult(
                False,
                f"Требуется уровень {item.required_level}.",
                item=item,
            )

        # Разворачиваем набор на компоненты
        components: list[ShopItem] = []
        if item.bundle:
            owned = owned_item_ids(guild_id, user_id)
            missing_ids = [cid for cid in item.bundle if cid not in owned]
            if not missing_ids:
                return PurchaseResult(
                    False, "Все компоненты набора уже у вас.", item=item
                )
            for cid in missing_ids:
                comp = shop_store.get_item(cid)
                if comp is None:
                    return PurchaseResult(
                        False, "Набор повреждён: компонент не найден.",
                        item=item,
                    )
                components.append(comp)
        else:
            owned = owned_item_ids(guild_id, user_id)
            if item_id in owned:
                return PurchaseResult(
                    False, "У вас уже есть этот товар.", item=item
                )
            components = [item]

        price = item.price
        op_id = shop_ops_store.new_op_id()
        try:
            await user_balance_store.subtract_balance(
                guild_id, user_id, price
            )
        except ValueError:
            return PurchaseResult(
                False,
                f"Недостаточно монет. Нужно: {price}.",
                item=item, insufficient=True,
            )
        except Exception:
            return PurchaseResult(
                False, "Ошибка базы данных при списании.", item=item
            )

        granted: list[str] = []
        try:
            for comp in components:
                ok, why = await _grant_item(
                    guild_id, user_id, comp, guild
                )
                if not ok:
                    raise RuntimeError(why or "grant failed")
                granted.append(comp.id)
        except Exception as exc:
            await _refund_once(guild_id, user_id, price, op_id)
            shop_ops_store.record(
                guild_id=guild_id, user_id=user_id, kind="purchase",
                item_id=item.id, price=price, status="refunded",
                detail=str(exc)[:200], op_id=op_id,
            )
            return PurchaseResult(
                True,  # деньги возвращены — операция «не потеряла» средства
                f"Выдача не удалась ({exc}). Монеты возвращены.",
                item=item, refunded=True,
            )

        shop_ops_store.record(
            guild_id=guild_id, user_id=user_id, kind="purchase",
            item_id=item.id, price=price, status="done",
            detail=("bundle:" + ",".join(granted)) if len(granted) > 1 else "",
            op_id=op_id,
        )
        return PurchaseResult(
            True, "Покупка успешна.", item=item, granted=granted
        )


async def activate_color_role(
    guild: discord.Guild,
    user_id: int,
    item_id: str,
) -> tuple[bool, str]:
    """Назначить Discord-роль цвета. Один активный цвет за раз.

    Не снимает купленные цвета из коллекции — только Discord-роли.
    Возвращает (ok, предупреждение/ошибка).
    """
    item = shop_store.get_item(item_id)
    if item is None or item.category != "color_roles" or not item.role_id:
        return False, "Это не цветная роль."

    async with await _user_lock(guild.id, user_id):
        owned = owned_item_ids(guild.id, user_id)
        if item_id not in owned:
            return False, "Вы ещё не приобрели этот цвет."

        # Все купленные цветные роли
        color_items = [
            i for i in shop_store.get_items_by_category("color_roles")
            if i.role_id and i.id in owned
        ]
        target_role = guild.get_role(item.role_id)
        if target_role is None:
            return False, "Роль цвета не найдена на сервере."
        bot_member = guild.me
        if bot_member is None:
            return False, "Бот ещё не готов."
        if not bot_member.guild_permissions.manage_roles:
            return False, "У бота нет права управления ролями."
        if target_role.position >= bot_member.top_role.position:
            return (
                False,
                "Роль цвета выше роли бота — назначить нельзя. "
                "Попросите администратора поднять роль бота.",
            )

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                return False, "Участник не найден."

        warnings: list[str] = []
        # Снять остальные активные цветные роли
        for other in color_items:
            if other.id == item_id or not other.role_id:
                continue
            other_role = guild.get_role(other.role_id)
            if other_role is None:
                continue
            if any(r.id == other.role_id for r in member.roles):
                if other_role.position >= bot_member.top_role.position:
                    warnings.append(
                        f"Не удалось снять {other.name}: "
                        "роль выше роли бота."
                    )
                    continue
                try:
                    await member.remove_roles(other_role)
                except discord.Forbidden:
                    warnings.append(
                        f"Нет прав снять {other.name}."
                    )
                except discord.HTTPException:
                    warnings.append(
                        f"Ошибка Discord при снятии {other.name}."
                    )

        try:
            await member.add_roles(target_role)
        except discord.Forbidden:
            return False, "Discord запретил назначение роли (права/иерархия)."
        except discord.HTTPException as exc:
            return False, f"Ошибка Discord: {exc.status}"

        # Экипировка в инвентаре: активный цвет один
        inventory_store.equip_cosmetic(guild.id, user_id, item_id)
        shop_ops_store.record(
            guild_id=guild.id, user_id=user_id, kind="equip",
            item_id=item_id, price=0, status="done",
            detail="color_role",
        )
        warn = (" " + " ".join(warnings)) if warnings else ""
        return True, warn


async def deactivate_color_role(
    guild: discord.Guild, user_id: int, item_id: str
) -> tuple[bool, str]:
    """Снять активный цвет (Discord-роль + экипировка)."""
    item = shop_store.get_item(item_id)
    if item is None or item.category != "color_roles" or not item.role_id:
        return False, "Это не цветная роль."
    async with await _user_lock(guild.id, user_id):
        role = guild.get_role(item.role_id)
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None
        if role is not None and member is not None and any(
            r.id == role.id for r in member.roles
        ):
            bot_member = guild.me
            if (
                bot_member is not None
                and role.position < bot_member.top_role.position
            ):
                try:
                    await member.remove_roles(role)
                except discord.HTTPException:
                    pass
        inventory_store.unequip_cosmetic(guild.id, user_id, item_id)
        return True, ""


# ---------------------------------------------------------------- pets/cases


async def purchase_pet(
    guild_id: int, user_id: int, pet_type: str, price: int
) -> tuple[bool, str, object | None]:
    """Купить питомца. (ok, сообщение, Pet|None)."""
    from storage.pet_store import pet_store

    async with await _user_lock(guild_id, user_id):
        if not await pet_store.has_pet_slot(user_id):
            return False, "У вас уже есть питомец.", None
        try:
            await user_balance_store.subtract_balance(
                guild_id, user_id, price
            )
        except ValueError:
            return False, f"Недостаточно монет. Нужно: {price}.", None
        except Exception:
            return False, "Ошибка базы данных.", None
        try:
            pet_def = pet_store._catalog.get_pet(pet_type)
            if pet_def is None:
                raise ValueError("unknown pet")
            pet = await pet_store.create_pet(
                owner_1_id=user_id,
                owner_2_id=None,
                pet_type=pet_type,
                pet_name=pet_type.replace("_", " ").title(),
            )
        except Exception as exc:
            await _refund_once(
                guild_id, user_id, price, shop_ops_store.new_op_id()
            )
            return False, f"Не удалось создать питомца ({exc}). Монеты возвращены.", None
        shop_ops_store.record(
            guild_id=guild_id, user_id=user_id, kind="pet",
            item_id=pet_type, price=price, status="done",
        )
        return True, "Питомец приобретён.", pet


async def open_case_safe(
    guild_id: int, user_id: int, case_id: str, guild: discord.Guild | None
) -> dict:
    """Открыть кейс под per-user lock; ошибки → {"type":"error"}."""
    from storage.case_store import case_store

    async with await _user_lock(guild_id, user_id):
        case = case_store.get_case(case_id)
        if case is None or not case.is_active:
            return {"type": "error", "value": "Кейс не найден"}
        try:
            result = await case_store.open_case(
                guild_id, user_id, case_id, guild
            )
        except ValueError:
            return {"type": "error", "value": "Недостаточно монет"}
        except Exception as exc:
            return {"type": "error", "value": f"Ошибка: {exc}"}
        if isinstance(result, dict) and result.get("type") == "error":
            return result
        shop_ops_store.record(
            guild_id=guild_id, user_id=user_id, kind="case",
            item_id=case_id, price=case.price, status="done",
            detail=str((result or {}).get("type", "")),
        )
        return result or {"type": "error", "value": "Нет результата"}


async def open_egg_safe(
    guild_id: int, user_id: int, price: int
) -> tuple[bool, str, object | None]:
    """Открыть яйцо загадки. (ok, сообщение, Pet|None)."""
    from models.pets_catalog import MYSTERY_EGG_DATA
    from storage.pet_store import pet_store

    async with await _user_lock(guild_id, user_id):
        if not await pet_store.has_pet_slot(user_id):
            return False, "У вас уже есть питомец — яйцо не нужно.", None
        try:
            await user_balance_store.subtract_balance(
                guild_id, user_id, price
            )
        except ValueError:
            return False, f"Недостаточно монет. Нужно: {price}.", None
        except Exception:
            return False, "Ошибка базы данных.", None
        try:
            drop = await pet_store.open_mystery_egg()
            pet_def = (
                drop.get("pet") if isinstance(drop, dict) else None
            )
            pet_type = pet_def.get("id") if isinstance(pet_def, dict) else None
            if pet_type:
                pet = await pet_store.create_pet(
                    owner_1_id=user_id,
                    owner_2_id=None,
                    pet_type=pet_type,
                    pet_name=pet_type.replace("_", " ").title(),
                )
            else:
                pet = None
        except Exception as exc:
            await _refund_once(
                guild_id, user_id, price, shop_ops_store.new_op_id()
            )
            return False, f"Ошибка открытия ({exc}). Монеты возвращены.", None
        shop_ops_store.record(
            guild_id=guild_id, user_id=user_id, kind="egg",
            item_id="mystery_egg", price=price, status="done",
            detail="won" if pet else "nothing",
        )
        if pet is None:
            return True, "Пусто — в следующий раз повезёт больше.", None
        return True, "Яйцо выдало питомца!", pet
