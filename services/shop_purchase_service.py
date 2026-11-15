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
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

logger = logging.getLogger(__name__)

from models.shop_item import PlayerCosmetic, ShopItem
from storage.shop_ops_store import shop_ops_store
from storage.shop_purchase_ops_store import purchase_ops_store
from storage.shop_store import inventory_store, shop_store

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
) -> dict[str, int]:
    """Строго идемпотентный возврат через durable-операцию.

    Никакого fallback на обычный add_balance: если
    add_balance_once реально выполнился, но ответ потерялся,
    повторный вызов с тем же ключом shopref:{op_id} вернёт
    зафиксированную сумму БЕЗ повторного начисления.
    Недоплата из-за MAX_BALANCE уходит в refund_pending
    и досчитывается recover_pending().
    """
    if amount <= 0:
        return {"credited": 0, "pending": 0}
    try:
        res = await purchase_ops_store.refund(
            op_id, guild_id, user_id, amount
        )
    except Exception:
        res = {"credited": 0, "pending": amount}
    return res


async def _record_refund(
    op_id: str, res: dict[str, int]
) -> None:
    """Зафиксировать результат возврата в durable-операции."""
    await purchase_ops_store.set_status(
        op_id, "refunded",
        payload={
            "refund_base": res.get("credited", 0),
            "refund_pending": res.get("pending", 0),
        },
    )


async def _revoke_grant(
    guild_id: int,
    user_id: int,
    item_id: str,
    guild: discord.Guild | None,
) -> None:
    """Компенсация: откатить выдачу, выполненную этой операцией.

    Вызывается только для предметов, которые операция реально
    добавила (они проверены как отсутствовавшие до покупки) —
    чужие/прежние предметы не затрагиваются.
    """
    item = shop_store.get_item(item_id)
    # Discord-роль для не-цветных role-предметов
    if (
        item is not None
        and item.item_type == "role"
        and item.category != "color_roles"
        and item.role_id
        and guild is not None
    ):
        try:
            from utils.role_manager import remove_role
            await remove_role(guild, user_id, item.role_id)
        except Exception:
            pass
    inventory_store.remove_cosmetic(guild_id, user_id, item_id)


async def purchase(
    guild_id: int,
    user_id: int,
    item_id: str,
    guild: discord.Guild | None = None,
) -> PurchaseResult:
    """Купить товар (или набор) через durable-операцию.

    reserved → charge (атомарно с балансом) → grant → done.
    Краш на любом шаге восстанавливается recover_pending():
    charged-операция довыдаётся или возвращается.
    """
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

        # Разворачиваем набор на недостающие компоненты
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
        op_id = purchase_ops_store.new_op_id()
        await purchase_ops_store.create(
            op_id, guild_id, user_id, "purchase", item.id, price,
            payload={
                "components": [c.id for c in components],
                "bundle": bool(item.bundle),
            },
        )

        charge = await purchase_ops_store.charge(
            op_id, guild_id, user_id, price
        )
        if charge == "insufficient":
            return PurchaseResult(
                False,
                f"Недостаточно монет. Нужно: {price}.",
                item=item, insufficient=True,
            )
        if charge == "exists":
            return PurchaseResult(
                False, "Эта покупка уже обработана.", item=item
            )
        if charge != "charged":
            return PurchaseResult(
                False, "Ошибка базы данных при списании.", item=item
            )

        # Выдача. При ошибке — компенсируем ТОЛЬКО выданное этой
        # операцией и возвращаем деньги идемпотентно.
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
            for comp_id in granted:
                try:
                    await _revoke_grant(guild_id, user_id, comp_id, guild)
                except Exception:
                    logger.exception("revoke grant failed: %s", comp_id)
            res = await _refund_once(guild_id, user_id, price, op_id)
            if res.get("pending", 0) > 0 and res.get("credited", 0) == 0:
                await purchase_ops_store.set_status(
                    op_id, "unknown",
                    payload={"refund_pending": price},
                )
            else:
                await _record_refund(op_id, res)
            shop_ops_store.record(
                guild_id=guild_id, user_id=user_id, kind="purchase",
                item_id=item.id, price=price, status="refunded",
                detail=str(exc)[:200], op_id=op_id,
            )
            return PurchaseResult(
                True,
                f"Выдача не удалась ({exc}). Монеты возвращены.",
                item=item, refunded=True,
            )

        await purchase_ops_store.set_status(
            op_id, "done",
            payload={"granted": granted},
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


async def _begin_op(
    kind: str, guild_id: int, user_id: int, item_id: str,
    price: int, payload: dict | None = None,
) -> tuple[str, str]:
    """Создать durable-операцию и атомарно списать.

    Возвращает (op_id, charge_result)."""
    op_id = purchase_ops_store.new_op_id()
    await purchase_ops_store.create(
        op_id, guild_id, user_id, kind, item_id, price,
        payload=payload or {},
    )
    charge = await purchase_ops_store.charge(
        op_id, guild_id, user_id, price
    )
    return op_id, charge


async def _fail_charge(charge: str, price: int):
    if charge == "insufficient":
        return False, f"Недостаточно монет. Нужно: {price}.", None
    if charge == "exists":
        return False, "Эта операция уже обработана.", None
    return False, "Ошибка базы данных.", None


async def _refund_op(
    op_id: str, guild_id: int, user_id: int, price: int
) -> dict[str, int]:
    res = await _refund_once(guild_id, user_id, price, op_id)
    if res.get("pending", 0) > 0 and res.get("credited", 0) == 0:
        await purchase_ops_store.set_status(
            op_id, "unknown", payload={"refund_pending": price}
        )
    else:
        await _record_refund(op_id, res)
    return res


async def purchase_pet(
    guild_id: int, user_id: int, pet_type: str, price: int
) -> tuple[bool, str, object | None]:
    """Купить питомца через durable-операцию."""
    from storage.pet_store import pet_store

    async with await _user_lock(guild_id, user_id):
        if not await pet_store.has_pet_slot(user_id):
            return False, "У вас уже есть питомец.", None
        op_id, charge = await _begin_op(
            "pet", guild_id, user_id, pet_type, price,
            payload={"pet_type": pet_type},
        )
        if charge != "charged":
            return await _fail_charge(charge, price)
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
            await _refund_op(op_id, guild_id, user_id, price)
            return False, f"Не удалось создать питомца ({exc}). Монеты возвращены.", None
        await purchase_ops_store.set_status(op_id, "done")
        shop_ops_store.record(
            guild_id=guild_id, user_id=user_id, kind="pet",
            item_id=pet_type, price=price, status="done",
        )
        return True, "Питомец приобретён.", pet


async def open_case_safe(
    guild_id: int, user_id: int, case_id: str, guild: discord.Guild | None
) -> dict:
    """Открыть кейс: durable charge → open_case(precharged) → done.

    При краше между charge и завершением recover_pending
    вернёт монеты."""
    from storage.case_store import case_store

    async with await _user_lock(guild_id, user_id):
        case = case_store.get_case(case_id)
        if case is None or not case.is_active:
            return {"type": "error", "value": "Кейс не найден"}
        op_id, charge = await _begin_op(
            "case", guild_id, user_id, case_id, case.price,
            payload={"case_id": case_id},
        )
        if charge != "charged":
            if charge == "insufficient":
                return {"type": "error", "value": "Недостаточно монет"}
            if charge == "exists":
                return {"type": "error", "value": "Операция уже обработана"}
            return {"type": "error", "value": "Ошибка базы данных"}
        try:
            result = await case_store.open_case(
                guild_id, user_id, case_id, guild, precharged=True
            )
        except Exception as exc:
            await _refund_op(op_id, guild_id, user_id, case.price)
            return {"type": "error", "value": f"Ошибка: {exc}"}
        if isinstance(result, dict) and result.get("type") == "error":
            await _refund_op(op_id, guild_id, user_id, case.price)
            return result
        await purchase_ops_store.set_status(
            op_id, "done",
            payload={"result": str((result or {}).get("type", ""))},
        )
        shop_ops_store.record(
            guild_id=guild_id, user_id=user_id, kind="case",
            item_id=case_id, price=case.price, status="done",
            detail=str((result or {}).get("type", "")),
        )
        return result or {"type": "error", "value": "Нет результата"}


async def open_egg_safe(
    guild_id: int, user_id: int, price: int
) -> tuple[bool, str, object | None]:
    """Открыть яйцо загадки через durable-операцию."""
    from storage.pet_store import pet_store

    async with await _user_lock(guild_id, user_id):
        if not await pet_store.has_pet_slot(user_id):
            return False, "У вас уже есть питомец — яйцо не нужно.", None
        op_id, charge = await _begin_op(
            "egg", guild_id, user_id, "mystery_egg", price,
        )
        if charge != "charged":
            return await _fail_charge(charge, price)
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
            await _refund_op(op_id, guild_id, user_id, price)
            return False, f"Ошибка открытия ({exc}). Монеты возвращены.", None
        await purchase_ops_store.set_status(
            op_id, "done", payload={"egg_result": "won" if pet else "nothing"}
        )
        shop_ops_store.record(
            guild_id=guild_id, user_id=user_id, kind="egg",
            item_id="mystery_egg", price=price, status="done",
            detail="won" if pet else "nothing",
        )
        if pet is None:
            return True, "Пусто — в следующий раз повезёт больше.", None
        return True, "Яйцо выдало питомца!", pet


# ------------------------------------------------------------- recovery


async def _recover_purchase_op(op: dict, bot) -> str:
    """Восстановить одну charged-операцию покупки. → статус."""
    gid, uid = op["guild_id"], op["user_id"]
    payload = op.get("payload") or {}
    components = payload.get("components") or [op["item_id"]]
    guild = bot.get_guild(gid) if bot is not None else None

    async with await _user_lock(gid, uid):
        owned = owned_item_ids(gid, uid)
        missing = [c for c in components if c not in owned]
        if not missing:
            await purchase_ops_store.set_status(op["op_id"], "done")
            return "done"
        # Довыдать недостающие компоненты (идемпотентно)
        granted_now: list[str] = []
        try:
            for cid in missing:
                comp = shop_store.get_item(cid)
                if comp is None:
                    continue
                ok, _ = await _grant_item(gid, uid, comp, guild)
                if ok:
                    granted_now.append(cid)
        except Exception:
            logger.exception("recovery grant failed op=%s", op["op_id"])
            for cid in granted_now:
                try:
                    await _revoke_grant(gid, uid, cid, guild)
                except Exception:
                    pass
            res = await _refund_once(gid, uid, op["price"], op["op_id"])
            if res.get("pending", 0) > 0 and res.get("credited", 0) == 0:
                await purchase_ops_store.set_status(
                    op["op_id"], "unknown",
                    payload={"refund_pending": op["price"]},
                )
                return "unknown"
            await _record_refund(op["op_id"], res)
            return "refunded"
        owned = owned_item_ids(gid, uid)
        still = [c for c in components if c not in owned]
        if still:
            # Не смогли выдать (напр. нет guild для роли) — charged, повторим позже
            return "charged"
        await purchase_ops_store.set_status(op["op_id"], "done")
        return "done"


async def _recover_pet_op(op: dict) -> str:
    """Восстановить pet/egg операцию."""
    gid, uid = op["guild_id"], op["user_id"]
    from storage.pet_store import pet_store
    async with await _user_lock(gid, uid):
        pet = await pet_store.get_user_pet(gid, uid)
        if pet is not None:
            await purchase_ops_store.set_status(op["op_id"], "done")
            return "done"
        # Питомца нет: для egg «nothing» — легитимный исход;
        # для pet — выдача не завершилась → вернуть деньги.
        if op["kind"] == "egg":
            payload = op.get("payload") or {}
            if payload.get("egg_result") == "nothing":
                await purchase_ops_store.set_status(op["op_id"], "done")
                return "done"
        res = await _refund_once(gid, uid, op["price"], op["op_id"])
        if res.get("pending", 0) > 0 and res.get("credited", 0) == 0:
            await purchase_ops_store.set_status(
                op["op_id"], "unknown",
                payload={"refund_pending": op["price"]},
            )
            return "unknown"
        await _record_refund(op["op_id"], res)
        return "refunded"


async def _recover_case_op(op: dict) -> str:
    """Восстановить case-операцию: вернуть деньги.

    Результат ролла невоспроизводим; безопасный выбор — refund
    (пользователь не теряет монеты)."""
    gid, uid = op["guild_id"], op["user_id"]
    res = await _refund_once(gid, uid, op["price"], op["op_id"])
    if res.get("pending", 0) > 0 and res.get("credited", 0) == 0:
        await purchase_ops_store.set_status(
            op["op_id"], "unknown",
            payload={"refund_pending": op["price"]},
        )
        return "unknown"
    await _record_refund(op["op_id"], res)
    return "refunded"


async def recover_pending(bot=None) -> int:
    """Восстановить незавершённые durable-операции после рестарта.

    Вызывается из bot.on_ready. Возвращает число обработанных.
    """
    ops = await purchase_ops_store.pending()
    resolved = 0
    for op in ops:
        try:
            status = op["status"]
            if status == "reserved":
                # charge() атомарен — reserved значит «денег не списано»
                await purchase_ops_store.set_status(
                    op["op_id"], "failed",
                    payload={"reason": "interrupted_before_charge"},
                )
                resolved += 1
                continue
            if status == "unknown":
                pending = await purchase_ops_store.settle_pending_refund(op)
                if pending <= 0:
                    resolved += 1
                continue
            if status == "charged":
                kind = op["kind"]
                if kind == "purchase":
                    await _recover_purchase_op(op, bot)
                elif kind in ("pet", "egg"):
                    await _recover_pet_op(op)
                elif kind == "case":
                    await _recover_case_op(op)
                resolved += 1
        except Exception:
            logger.exception("shop ops recovery failed op=%s", op.get("op_id"))
    # Досчитать pending-возвраты у завершённых операций
    try:
        recent = await purchase_ops_store.recent(limit=500)
        for op in recent:
            payload = op.get("payload") or {}
            if payload.get("refund_pending", 0) > 0:
                await purchase_ops_store.settle_pending_refund(op)
    except Exception:
        logger.exception("pending refund sweep failed")
    return resolved