"""Призы и выплаты спонтанных событий 2.0.

Ровно четыре типа наград (других нет):

- ``coins`` — user_balance_store.add_balance_once (grant_key);
- ``xp``    — player_stats_store.add_xp_once (player_xp_grants);
- ``rep``   — reputation_store.award_rep_logged (event_key,
              источник EVENT — журнал и rep_balance синхронно);
- ``item``  — фиксированный ID предмета магазина, выбранный ДО
  публикации карточки; выдача в инвентарь идемпотентна по
  item_id. Дубликат (предмет уже в инвентаре) разрешается
  компенсацией монетами ``events.item_dup_compensation_coins``
  через отдельный идемпотентный grant_key ``<key>:comp`` —
  «пустая» награда успехом не объявляется, экономика не меняется
  молча (сумма настраивается в Control Center).

Выбор приза — ДО публикации карточки и ДО создания события:
тип по весам ``events.reward_weights``, сумма по диапазонам
настроек. «Растущий куш» — только числовые типы (coins/xp/rep),
веса item исключаются и перенормируются; диапазон —
start..amount (видимый рост).

Предметы магазина — allowlist-политика (documented):
- ``item_type == "cosmetic"`` — роли/услуги/привилегии не выдаются;
- ``is_active`` и сезонное окно ``available_from/until`` актуальны;
- ``required_level == 0`` — требование уровня не обходится;
- ``bundle is None`` — наборы не раздаются по частям;
- категория не входит в ``events.item_exclude_categories``
  (по умолчанию roles/access/privilege/admin).
Если допустимых предметов нет — тип item исключается из розыгрыша
ДО публикации (перевыбор среди оставшихся типов). Уже
зафиксированный приз никогда молча не меняется.

Приз — не покупка: двойная цена (монеты+REP-тариф
``rep_price_for_item``) применяется только к покупкам в магазине
и НЕ является ограничением eligibility приза (иначе весь
каталог — все предметы платные — был бы недоступен, что
фактически убило бы тип item). REP-начисление приза идёт через
``award_rep_logged`` с source=EVENT — те же журнальные
гарантии, что у остальных источников.
"""

from __future__ import annotations

import logging
import random
from datetime import date
from typing import Any

from services import event_config

logger = logging.getLogger(__name__)

REWARD_TYPES = ("coins", "xp", "rep", "item")

_PUBLIC_LABELS = {
    "coins": "монет",
    "xp": "XP",
    "rep": "REP",
    "item": "предмет из магазина",
}


def _in_season(item, today: date) -> bool:
    """Сезонное окно available_from/until (ISO-даты, None = всегда)."""
    af, au = item.available_from, item.available_until
    if af:
        try:
            if today < date.fromisoformat(str(af)[:10]):
                return False
        except ValueError:
            pass
    if au:
        try:
            if today > date.fromisoformat(str(au)[:10]):
                return False
        except ValueError:
            pass
    return True


def eligible_items(exclude_categories: list[str] | None = None
                   ) -> list[Any]:
    """Allowlist предметов, допустимых как приз (см. docstring)."""
    from storage.shop_store import shop_store
    excluded = {str(c).lower() for c in (exclude_categories or [])}
    out = []
    for item in shop_store.get_all_items():
        if not getattr(item, "is_active", False):
            continue
        if getattr(item, "item_type", "cosmetic") != "cosmetic":
            continue
        if getattr(item, "bundle", None):
            continue
        if int(getattr(item, "required_level", 0) or 0) > 0:
            continue
        cat = str(getattr(item, "category", "") or "").lower()
        if cat in excluded or any(c in cat for c in excluded if len(c) > 3):
            continue
        if not _in_season(item, date.today()):
            continue
        out.append(item)
    return out


def _roll_amount(rng: random.Random, lo: int, hi: int) -> int:
    lo, hi = event_config.clamp_range(int(lo), int(hi))
    return rng.randint(max(1, lo), max(1, hi))


def select_prize(guild_id: int | None,
                 settings: dict[str, Any],
                 game: str,
                 rng: random.Random) -> dict[str, Any] | None:
    """Выбрать и «зафиксировать» приз до публикации.

    Возвращает {"type", "amount", "item_id", "label",
    "start"?} или None, если ни один тип не может быть оплачен
    (пустой каталог для item + нулевые веса остальных).
    """
    weights = settings.get("reward_weights") or {}
    if game == "growing":
        # Только числовые типы; item-вес исключается и веса
        # перенормируются на coins/xp/rep.
        candidates = [t for t in ("coins", "xp", "rep")]
    else:
        candidates = list(REWARD_TYPES)

    # item без eligible-каталога исключается ДО выбора типа.
    items = []
    if "item" in candidates:
        items = eligible_items(settings.get("item_exclude_categories"))
        if not items:
            candidates = [c for c in candidates if c != "item"]

    w = {t: max(0.0, float(weights.get(t, 0.0) or 0.0))
         for t in candidates}
    # Нулевой вес у всех → равные веса (защита от пустого dict).
    if sum(w.values()) <= 0:
        w = {t: 1.0 for t in candidates}
    total = sum(w.values())
    r = rng.uniform(0, total)
    acc = 0.0
    rtype = candidates[-1]
    for t in candidates:
        acc += w[t]
        if r <= acc:
            rtype = t
            break

    if rtype == "coins":
        amount = _roll_amount(rng, settings.get("coins_min", 100),
                              settings.get("coins_max", 500))
        prize = {"type": "coins", "amount": amount, "item_id": None,
                 "label": f"{amount:,} монет"}
    elif rtype == "xp":
        amount = _roll_amount(rng, settings.get("xp_min", 50),
                              settings.get("xp_max", 200))
        prize = {"type": "xp", "amount": amount, "item_id": None,
                 "label": f"{amount} XP"}
    elif rtype == "rep":
        amount = _roll_amount(rng, settings.get("rep_min", 1),
                              settings.get("rep_max", 3))
        prize = {"type": "rep", "amount": amount, "item_id": None,
                 "label": f"{amount} REP"}
    elif rtype == "item":
        item = rng.choice(items)
        # Компенсация зафиксирована В ПРИЗЕ до публикации: изменение
        # events.item_dup_compensation_coins позже не меняет
        # уже зафиксированное обязательство.
        dup_comp = int(event_config._peek(
            guild_id, "events.item_dup_compensation_coins",
            100) or 0)
        prize = {"type": "item", "amount": 0,
                 "dup_comp": max(0, dup_comp),
                 "item_id": str(item.id),
                 "item_name": str(item.name),
                 "label": "🎁 предмет из магазина"}
    else:
        return None

    if game == "growing" and prize["type"] != "item":
        # Растущий куш: start..amount, видимый рост к дедлайну.
        # start — случайная точка нижней половины диапазона,
        # amount — уже выпавший максимум.
        lo = {"coins": settings.get("coins_min", 100),
              "xp": settings.get("xp_min", 50),
              "rep": settings.get("rep_min", 1)}[prize["type"]]
        hi = prize["amount"]
        start_hi = max(int(lo), int(hi) // 2)
        prize["start"] = rng.randint(int(lo), max(int(lo), start_hi))
        prize["label"] = (f"{prize['start']} → {prize['amount']} "
                          f"{_PUBLIC_LABELS[prize['type']]}")
    return prize


def prize_public_label(prize: dict[str, Any]) -> str:
    return str(prize.get("label") or prize.get("type") or "")


# ------------------------------------------------------------------
# Гранты (вызываются из cog; протокол как у grant_drop_reward)
# ------------------------------------------------------------------

def _inventory_file_has(guild_id: int, user_id: int,
                        item_id: str) -> bool | None:
    """Проверка durable-состояния инвентаря НАПРЯМУЮ из файла.

    In-memory кэш InventoryStore может содержать «фантомный»
    предмет после неудачного save() — подтверждением выдачи
    считается только запись на диске. None — файл нечитаем
    (вызывающий обязан повторить, а не считать выдачу успешной).
    """
    import json as _json
    from storage.shop_store import INVENTORY_FILE
    try:
        data = _json.loads(INVENTORY_FILE.read_text(
            encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception:
        return None
    key = f"{guild_id}:{user_id}"
    for c in data.get(key, []) or []:
        if isinstance(c, dict) and c.get("item_id") == item_id:
            return True
    return False


async def _settle_item(guild_id: int, user_id: int,
                       item_id: str,
                       mode: str,
                       comp_key: str | None = None,
                       comp_amount: int = 0) -> str:
    """Исполнить item-обязательство по зафиксированному ``mode``.

    ``mode`` приходит из ``event_item_marks`` — решение
    «предмет или компенсация» сохранено ДО побочных эффектов,
    поэтому retry после крэша не выдаст и предмет, и
    компенсацию за одну награду. Возвращает ``item`` |
    ``comp:<amount>`` | ``owned``.
    """
    from models.shop_item import PlayerCosmetic
    from storage.shop_store import inventory_store, shop_store
    item = shop_store.get_item(item_id)
    if item is None or not getattr(item, "is_active", False):
        raise RuntimeError(
            f"event item prize unavailable: {item_id}")

    def _cosmetic() -> "PlayerCosmetic":
        return PlayerCosmetic(
            guild_id=guild_id, user_id=user_id,
            item_id=item_id, equipped=False)

    if mode == "comp":
        if not comp_key or comp_amount <= 0:
            return "owned"
        from storage.user_balance_store import (
            user_balance_store)
        if not user_balance_store._use_db:
            raise RuntimeError(
                "item dup compensation requires PostgreSQL")
        await user_balance_store.add_balance_once(
            guild_id, user_id, comp_amount, comp_key)
        logger.info(
            "event item dup compensated: guild=%s user=%s "
            "item=%s +%s coins", guild_id, user_id,
            item_id, comp_amount)
        return f"comp:{comp_amount}"

    if mode != "item":
        raise RuntimeError(f"unknown item mark mode: {mode}")

    owned = _inventory_file_has(guild_id, user_id, item_id)
    if owned is True:
        # Файл уже содержит предмет: либо наш прошлый grant
        # (крэш между save и complete — НАША выдача), либо
        # параллельный grant того же item_id — в обоих случаях
        # предмет у игрока есть, обязательство 'item' исполнено.
        # Компенсация не начисляется — mark 'comp' отдельный.
        return "owned"
    if owned is None:
        raise RuntimeError("inventory file unreadable")

    try:
        added = inventory_store.add_cosmetic(_cosmetic())
    except Exception:
        # save() мог упасть после мутации памяти — сбрасываем
        # фантомное состояние, чтобы retry не увидел ложный дубль.
        try:
            inventory_store.load()
        except Exception:
            pass
        raise
    if added:
        # save() отработал без исключения — подтверждаем файлом.
        if _inventory_file_has(guild_id, user_id, item_id) is True:
            return "item"
        raise RuntimeError("item grant not durable after save")
    # added=False: либо настоящий дубль (owned=True выше — уже
    # вернулись), либо фантом в памяти. Перечитываем файл и
    # при необходимости повторяем выдачу один раз.
    if _inventory_file_has(guild_id, user_id, item_id) is True:
        return "owned"
    try:
        inventory_store.load()  # выкинуть фантом из памяти
    except Exception:
        pass
    inventory_store.add_cosmetic(_cosmetic())  # исключение → retry
    if _inventory_file_has(guild_id, user_id, item_id) is not True:
        raise RuntimeError("item grant not durable after retry")
    return "item"


async def apply_reward(row: dict[str, Any],
                       user_name: str = "") -> str:
    """Применить одну награду по строке event_rewards.

    Идемпотентно по grant_key на уровне целевого хранилища.
    Бросает исключение при сбое — worker повторит с backoff.
    Возвращает строку фактического результата для
    event_rewards.result (``coins``/``xp``/``rep``/``item``/
    ``owned``/``comp:<amount>``).
    """
    kind = str(row.get("kind") or "")
    gid = int(row["guild_id"])
    uid = int(row["user_id"])
    amount = int(row.get("amount") or 0)
    key = str(row["grant_key"])

    if kind == "coins":
        from storage.user_balance_store import user_balance_store
        if not user_balance_store._use_db:
            raise RuntimeError("coin grants require PostgreSQL")
        await user_balance_store.add_balance_once(gid, uid, amount, key)
        return "coins"

    if kind == "xp":
        from storage.player_stats_store import player_stats_store
        await player_stats_store.add_xp_once(
            gid, uid, amount, key, name=user_name)
        return "xp"

    if kind == "rep":
        from storage.reputation_store import (
            SRC_EVENT, reputation_store)
        res = await reputation_store.award_rep_logged(
            guild_id=gid, user_id=uid, source=SRC_EVENT,
            amount=amount, event_key=key,
            actor_user_id=0, subject_user_id=uid,
            user_name=user_name)
        if res is None:
            # event_key уже был — награда выдана ранее.
            return "rep"
        return "rep"

    if kind == "item":
        item_id = row.get("item_id")
        if not item_id:
            raise RuntimeError("item reward without item_id")
        from storage import event_reward_store as _ers
        # Способ исполнения фиксируется ДО побочных эффектов:
        # 'item' — предмет должен быть выдан этой наградой;
        # 'comp' — предмет уже был у игрока → компенсация.
        # comp_amount заморожен в event_rewards.amount при
        # record() — смена настройки не меняет обязательство.
        comp_amount = max(0, int(row.get("amount") or 0))
        mark = await _ers.event_reward_store.get_item_mark(key)
        if mark is None:
            owned = _inventory_file_has(gid, uid, str(item_id))
            if owned is None:
                raise RuntimeError("inventory file unreadable")
            mode = "comp" if owned else "item"
            mark = await _ers.event_reward_store.put_item_mark(
                key, mode, comp_amount if mode == "comp" else 0)
        mode = str((mark or {}).get("mode") or "item")
        mark_comp = int((mark or {}).get("comp_amount") or 0)
        return await _settle_item(
            gid, uid, str(item_id), mode,
            comp_key=f"{key}:comp",
            comp_amount=mark_comp or comp_amount)

    raise RuntimeError(f"unknown event reward kind: {kind}")
