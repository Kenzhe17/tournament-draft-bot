"""Призы и выплаты спонтанных событий 2.0.

Ровно четыре типа наград (других нет):

- ``coins`` — user_balance_store.add_balance_once (grant_key);
- ``xp``    — player_stats_store.add_xp_once (player_xp_grants);
- ``rep``   — reputation_store.award_rep_logged (event_key,
              источник EVENT — журнал и rep_balance синхронно);
- ``item``  — фиксированный ID предмета магазина, выбранный ДО
  публикации карточки; выдача в инвентарь идемпотентна по
  item_id (повторная выдача того же предмета — no-op).

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
        prize = {"type": "item", "amount": 0,
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

async def _settle_item(guild_id: int, user_id: int,
                       item_id: str) -> None:
    """Выдать предмет в инвентарь идемпотентно.

    add_cosmetic дедуплицирует по item_id: повторная обработка
    после потерянного complete вернёт False — это НЕ ошибка,
    предмет уже у игрока. Отсутствующий/неактивный предмет —
    ошибка (retry), приз не подменяется.
    """
    from models.shop_item import PlayerCosmetic
    from storage.shop_store import inventory_store, shop_store
    item = shop_store.get_item(item_id)
    if item is None or not getattr(item, "is_active", False):
        raise RuntimeError(
            f"event item prize unavailable: {item_id}")
    inventory_store.add_cosmetic(PlayerCosmetic(
        guild_id=guild_id, user_id=user_id,
        item_id=item_id, equipped=False))


async def apply_reward(row: dict[str, Any],
                       user_name: str = "") -> None:
    """Применить одну награду по строке event_rewards.

    Идемпотентно по grant_key на уровне целевого хранилища.
    Бросает исключение при сбое — worker повторит с backoff.
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
        return

    if kind == "xp":
        from storage.player_stats_store import player_stats_store
        await player_stats_store.add_xp_once(
            gid, uid, amount, key, name=user_name)
        return

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
            return
        return

    if kind == "item":
        item_id = row.get("item_id")
        if not item_id:
            raise RuntimeError("item reward without item_id")
        await _settle_item(gid, uid, str(item_id))
        return

    raise RuntimeError(f"unknown event reward kind: {kind}")
