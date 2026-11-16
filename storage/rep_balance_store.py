"""Расходуемый баланс REP (Reputation 2.0 → SHOP 2.0).

`player_stats.reputation` остаётся пожизненным показателем заработанной
репутации (рейтинг, профили, история). Здесь — отдельный расходуемый
баланс в PostgreSQL:

    rep_balance(guild_id, user_id):
        balance       — доступно для трат
        earned_total  — всего начислено в расходуемый баланс
        spent_total   — всего потрачено в магазине

Миграция: при первом обращении к пользователю без строки
баланс инициализируется из player_stats.reputation — однократно
и идемпотентно (строка создаётся один раз, дальше живёт сама).

Все изменения earned/spent идут в тех же транзакциях, что и
начисления репутации (reputation_store) или списания магазина
(shop_purchase_ops_store). JSON-fallback для REP-операций
не используется — только PostgreSQL.

Возвраты REP идемпотентны через rep_balance_grants
(ключ shoprefrep:{op_id} / shoprefprep:{op_id}:{n}).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS rep_balance (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    earned_total INTEGER NOT NULL DEFAULT 0,
    spent_total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
)
"""
GRANT_DDL = """
CREATE TABLE IF NOT EXISTS rep_balance_grants (
    grant_key TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    amount INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# ------------------------------------------------------------------ цены


def rep_price_for_coins(coins: int) -> int:
    """REP-стоимость покупки по цене в монетах.

    Тарифы:
      0 монет (бесплатно) → 0 REP
      1–999 монет    → 1 REP
      1000–2999      → 2 REP
      3000–5999      → 3 REP
      6000–14999     → 5 REP
      15000+         → 8 REP
    """
    if coins <= 0:
        return 0
    if coins < 1000:
        return 1
    if coins < 3000:
        return 2
    if coins < 6000:
        return 3
    if coins < 15000:
        return 5
    return 8


def rep_price_for_item(item) -> int:
    """REP-стоимость ShopItem: фиксированная rep_price или тариф.

    Бесплатный товар (price<=0) всегда стоит 0 REP. Для платного
    товара rep_price=0 недопустим — обязательная двойная оплата
    не может быть обойдена через админский override.
    """
    price = int(getattr(item, "price", 0) or 0)
    if price <= 0:
        return 0
    override = getattr(item, "rep_price", None)
    if override is not None:
        return max(1, int(override))
    return rep_price_for_coins(price)


def rep_price_for_amount(coins: int, rep_price: int | None = None) -> int:
    """REP-стоимость для не-ShopItem покупок (кейсы, питомцы, яйца,
    гардероб, услуги): тариф от суммы или явный override."""
    if rep_price is not None:
        return max(0, int(rep_price))
    coins = int(coins or 0)
    if coins <= 0:
        return 0
    return rep_price_for_coins(coins)


# ------------------------------------------------------------------- store


class RepBalanceStore:
    def __init__(self) -> None:
        self._ddl_done = False

    async def _ensure_ddl(self, conn) -> None:
        if not self._ddl_done:
            await conn.execute(DDL)
            await conn.execute(GRANT_DDL)
            self._ddl_done = True

    async def _conn(self):
        from storage.db import get_pool
        return await get_pool()

    # ---------------------------------------------------------- migration

    async def _migrate_and_lock(self, conn, guild_id: int, user_id: int) -> int:
        """Гарантировать строку rep_balance (миграция из reputation).

        Возвращает доступный баланс. Вызывается внутри транзакции —
        строка читается FOR UPDATE, отсутствующая создаётся из
        player_stats.reputation.
        """
        await self._ensure_ddl(conn)
        row = await conn.fetchrow(
            """
            SELECT balance FROM rep_balance
            WHERE guild_id=$1 AND user_id=$2
            FOR UPDATE
            """,
            guild_id, user_id,
        )
        if row is not None:
            return int(row["balance"])
        # Одноразовая идемпотентная миграция: доступно = заработано.
        rep_total = await conn.fetchval(
            "SELECT reputation FROM player_stats "
            "WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
        rep_total = int(rep_total or 0)
        await conn.execute(
            """
            INSERT INTO rep_balance
                (guild_id, user_id, balance, earned_total, spent_total)
            VALUES ($1, $2, $3, $3, 0)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            """,
            guild_id, user_id, rep_total,
        )
        row = await conn.fetchrow(
            "SELECT balance FROM rep_balance "
            "WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
        return int(row["balance"]) if row else rep_total

    # ------------------------------------------------------------ queries

    async def get_balance(self, guild_id: int, user_id: int) -> int:
        pool = await self._conn()
        async with pool.acquire() as conn:
            async with conn.transaction():
                return await self._migrate_and_lock(conn, guild_id, user_id)

    async def get_info(self, guild_id: int, user_id: int) -> dict[str, int]:
        """{balance, earned_total, spent_total} с миграцией."""
        pool = await self._conn()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._migrate_and_lock(conn, guild_id, user_id)
                row = await conn.fetchrow(
                    "SELECT balance, earned_total, spent_total "
                    "FROM rep_balance WHERE guild_id=$1 AND user_id=$2",
                    guild_id, user_id,
                )
                if row is None:
                    return {"balance": 0, "earned_total": 0, "spent_total": 0}
                return {
                    "balance": int(row["balance"]),
                    "earned_total": int(row["earned_total"]),
                    "spent_total": int(row["spent_total"]),
                }

    # ------------------------------------------------- синхронизация award

    async def sync_after_rep_change(
        self,
        conn,
        guild_id: int,
        user_id: int,
        rep_after: int,
        delta: int,
    ) -> None:
        """Обновить расходуемый баланс после изменения reputation.

        Вызывается внутри транзакции reputation_store:
        - rep_after — player_stats.reputation после апдейта;
        - delta — изменение этого начисления (может быть <0 у admin).

        Если строки ещё нет — миграция из rep_after (потраченного
        ещё не было, spendable = заработано).
        Если есть — balance += delta (clamp ≥0), earned += max(delta,0).
        """
        await self._ensure_ddl(conn)
        row = await conn.fetchrow(
            "SELECT balance, earned_total FROM rep_balance "
            "WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
            guild_id, user_id,
        )
        if row is None:
            await conn.execute(
                """
                INSERT INTO rep_balance
                    (guild_id, user_id, balance, earned_total, spent_total)
                VALUES ($1, $2, $3, $3, 0)
                ON CONFLICT (guild_id, user_id) DO NOTHING
                """,
                guild_id, user_id, int(rep_after),
            )
            return
        await conn.execute(
            """
            UPDATE rep_balance
            SET balance = GREATEST(balance + $3, 0),
                earned_total = earned_total + GREATEST($3, 0)
            WHERE guild_id=$1 AND user_id=$2
            """,
            guild_id, user_id, int(delta),
        )

    # ------------------------------------------------------------- траты

    async def spend_in_tx(
        self, conn, guild_id: int, user_id: int, amount: int
    ) -> bool:
        """Списать REP внутри чужой транзакции. False — не хватает."""
        if amount <= 0:
            return True
        balance = await self._migrate_and_lock(conn, guild_id, user_id)
        if balance < amount:
            return False
        await conn.execute(
            """
            UPDATE rep_balance
            SET balance = balance - $3,
                spent_total = spent_total + $3
            WHERE guild_id=$1 AND user_id=$2
            """,
            guild_id, user_id, amount,
        )
        return True

    # ------------------------------------------------------------ возвраты

    async def add_rep_once(
        self, guild_id: int, user_id: int, amount: int, grant_key: str
    ) -> int:
        """Идемпотентное начисление REP (возврат за покупку).

        Возвращает фактически зачисленную сумму: при повторном вызове
        с тем же grant_key — зафиксированную ранее, баланс не меняется.
        Учитывает spent_total (возврат уменьшает потраченное).
        """
        if amount <= 0:
            return 0
        pool = await self._conn()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._ensure_ddl(conn)
                row = await conn.fetchrow(
                    """
                    INSERT INTO rep_balance_grants
                        (grant_key, guild_id, user_id, amount)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (grant_key) DO NOTHING
                    RETURNING amount
                    """,
                    grant_key, guild_id, user_id, amount,
                )
                if row is None:
                    existing = await conn.fetchval(
                        "SELECT amount FROM rep_balance_grants "
                        "WHERE grant_key=$1",
                        grant_key,
                    )
                    return int(existing or 0)
                # Новый грант: вернуть REP и скорректировать spent_total
                await self._migrate_and_lock(conn, guild_id, user_id)
                await conn.execute(
                    """
                    UPDATE rep_balance
                    SET balance = balance + $3,
                        spent_total = GREATEST(spent_total - $3, 0)
                    WHERE guild_id=$1 AND user_id=$2
                    """,
                    guild_id, user_id, amount,
                )
                return amount

    # ----------------------------------------------------- история трат

    async def get_spend_history(
        self, guild_id: int, user_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Последние покупки с REP-стоимостью (из shop_operations)."""
        from storage.shop_purchase_ops_store import purchase_ops_store
        ops = await purchase_ops_store.recent(
            guild_id=guild_id, limit=200
        )
        out = []
        for op in ops:
            if op.get("user_id") != user_id:
                continue
            payload = op.get("payload") or {}
            rep = int(payload.get("rep_amount") or 0)
            if rep <= 0 or op.get("status") not in ("done", "refunded"):
                continue
            out.append({
                "op_id": op["op_id"],
                "kind": op.get("kind"),
                "item_id": op.get("item_id"),
                "price": op.get("price"),
                "rep": rep,
                "status": op.get("status"),
                "created_at": op.get("created_at"),
            })
            if len(out) >= limit:
                break
        return out


rep_balance_store = RepBalanceStore()
