"""Долговечное хранилище операций покупок магазина (PostgreSQL).

Таблица `shop_operations` — источник истины для финансовых
операций SHOP 2.0. Жизненный цикл:

    reserved → charged → done
                  ↘ refunded | failed | unknown

Ключевые гарантии:
- `charge()` выполняет списание баланса И перевод операции в
  `charged` в ОДНОЙ транзакции PG — либо оба, либо ни одного.
  Краш между ними невозможен по построению.
- `op_id` — PK: повторная обработка той же операции безопасна.
- Возвраты строго идемпотентны через `add_balance_once`
  с ключом `shopref:{op_id}`; недоплата из-за MAX_BALANCE
  фиксируется как `refund_pending` и досчитывается
  последующими попытками `shoprefp:{op_id}:{n}`.
- JSON-фолбэк `data/shop_purchase_ops.json` — на случай
  недоступности PG (операции всё равно durable на диске).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from config import DATA_DIR, MAX_BALANCE
from storage.json_atomic import atomic_write_json

logger = logging.getLogger(__name__)

OPS_JSON = DATA_DIR / "shop_purchase_ops.json"

DDL = """
CREATE TABLE IF NOT EXISTS shop_operations (
    op_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    price INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
INDEX_DDL = """
CREATE INDEX IF NOT EXISTS shop_operations_pending_idx
ON shop_operations (status)
WHERE status IN ('reserved', 'charged', 'unknown')
"""

PENDING_STATUSES = ("reserved", "charged", "unknown")


class ShopPurchaseOpsStore:
    def __init__(self) -> None:
        self._use_db = True
        self._ddl_done = False
        self._json_ops: dict[str, dict[str, Any]] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load_json()

    # ------------------------------------------------------------- json io

    def _load_json(self) -> None:
        if not OPS_JSON.exists():
            return
        try:
            data = json.loads(OPS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._json_ops = {
                    k: v for k, v in data.items() if isinstance(v, dict)
                }
        except (json.JSONDecodeError, OSError):
            self._json_ops = {}

    def _save_json(self) -> None:
        atomic_write_json(OPS_JSON, self._json_ops)

    @staticmethod
    def new_op_id() -> str:
        return uuid.uuid4().hex[:20]

    # ------------------------------------------------------------------ db

    async def _ensure_ddl(self, conn) -> None:
        if not self._ddl_done:
            await conn.execute(DDL)
            await conn.execute(INDEX_DDL)
            self._ddl_done = True

    async def _pool(self):
        from storage.db import get_pool
        return await get_pool()

    # ---------------------------------------------------------------- ops

    async def create(
        self,
        op_id: str,
        guild_id: int,
        user_id: int,
        kind: str,
        item_id: str,
        price: int,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Создать операцию reserved. Идемпотентно по op_id."""
        payload = dict(payload or {})
        if self._use_db:
            try:
                pool = await self._pool()
                async with pool.acquire() as conn:
                    await self._ensure_ddl(conn)
                    row = await conn.fetchrow(
                        """
                        INSERT INTO shop_operations
                            (op_id, guild_id, user_id, kind, item_id,
                             price, status, payload)
                        VALUES ($1,$2,$3,$4,$5,$6,'reserved',$7::jsonb)
                        ON CONFLICT (op_id) DO NOTHING
                        RETURNING *
                        """,
                        op_id, guild_id, user_id, kind, item_id,
                        price, json.dumps(payload),
                    )
                    if row is None:
                        row = await conn.fetchrow(
                            "SELECT * FROM shop_operations WHERE op_id=$1",
                            op_id,
                        )
                    return self._row_to_op(row)
            except Exception:
                logger.exception("shop ops create: PG failed, json fallback")
                self._use_db = False
        op = {
            "op_id": op_id, "guild_id": guild_id, "user_id": user_id,
            "kind": kind, "item_id": item_id, "price": price,
            "status": "reserved", "payload": payload,
            "created_at": time.time(), "updated_at": time.time(),
        }
        self._json_ops[op_id] = op
        self._save_json()
        return dict(op)

    @staticmethod
    def _row_to_op(row) -> dict[str, Any]:
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        return {
            "op_id": row["op_id"],
            "guild_id": row["guild_id"],
            "user_id": row["user_id"],
            "kind": row["kind"],
            "item_id": row["item_id"],
            "price": row["price"],
            "status": row["status"],
            "payload": payload or {},
            "created_at": row["created_at"].timestamp()
            if hasattr(row["created_at"], "timestamp") else row["created_at"],
            "updated_at": row["updated_at"].timestamp()
            if hasattr(row["updated_at"], "timestamp") else row["updated_at"],
        }

    async def get(self, op_id: str) -> dict[str, Any] | None:
        if self._use_db:
            try:
                pool = await self._pool()
                async with pool.acquire() as conn:
                    await self._ensure_ddl(conn)
                    row = await conn.fetchrow(
                        "SELECT * FROM shop_operations WHERE op_id=$1", op_id
                    )
                    return self._row_to_op(row) if row else None
            except Exception:
                logger.exception("shop ops get: PG failed")
                return None
        op = self._json_ops.get(op_id)
        return dict(op) if op else None

    async def set_status(
        self,
        op_id: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Перевести статус; payload мёржится (jsonb ||)."""
        if self._use_db:
            try:
                pool = await self._pool()
                async with pool.acquire() as conn:
                    await self._ensure_ddl(conn)
                    if payload:
                        await conn.execute(
                            """
                            UPDATE shop_operations
                            SET status=$2, updated_at=now(),
                                payload = payload || $3::jsonb
                            WHERE op_id=$1
                            """,
                            op_id, status, json.dumps(payload),
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE shop_operations
                            SET status=$2, updated_at=now()
                            WHERE op_id=$1
                            """,
                            op_id, status,
                        )
                    return
            except Exception:
                logger.exception("shop ops set_status: PG failed")
        op = self._json_ops.get(op_id)
        if op is not None:
            op["status"] = status
            if payload:
                op["payload"].update(payload)
            op["updated_at"] = time.time()
            self._save_json()

    # --------------------------------------------------------------- charge

    async def charge(
        self,
        op_id: str,
        guild_id: int,
        user_id: int,
        amount: int,
    ) -> str:
        """Атомарно: списать баланс + перевести op reserved→charged.

        Возвращает:
          "charged"    — деньги списаны, операция charged
          "insufficient" — не хватило (op → failed)
          "exists"     — op уже charged/done (повторная обработка)
          "missing"    — op не найден
          "error"      — ошибка БД
        """
        if amount < 0:
            return "error"
        if self._use_db:
            try:
                from storage.user_balance_store import (
                    user_balance_store,
                )
                pool = await self._pool()
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await self._ensure_ddl(conn)
                        op_row = await conn.fetchrow(
                            "SELECT status FROM shop_operations "
                            "WHERE op_id=$1 FOR UPDATE",
                            op_id,
                        )
                        if op_row is None:
                            return "missing"
                        if op_row["status"] != "reserved":
                            return "exists"
                        # То же списание, что subtract_balance:
                        # ensure → FOR UPDATE → decay → check → update
                        await user_balance_store._ensure_row(
                            conn, guild_id, user_id
                        )
                        bal = await conn.fetchrow(
                            "SELECT balance FROM user_balance "
                            "WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
                            guild_id, user_id,
                        )
                        balance = await user_balance_store._apply_decay(
                            conn, guild_id, user_id, bal["balance"]
                        )
                        if balance < amount:
                            await conn.execute(
                                """
                                UPDATE shop_operations
                                SET status='failed', updated_at=now(),
                                    payload = payload ||
                                        '{"reason":"insufficient"}'::jsonb
                                WHERE op_id=$1
                                """,
                                op_id,
                            )
                            return "insufficient"
                        await conn.execute(
                            "UPDATE user_balance SET balance=$1 "
                            "WHERE guild_id=$2 AND user_id=$3",
                            balance - amount, guild_id, user_id,
                        )
                        await conn.execute(
                            """
                            UPDATE shop_operations
                            SET status='charged', updated_at=now(),
                                payload = payload ||
                                    $2::jsonb
                            WHERE op_id=$1
                            """,
                            op_id,
                            json.dumps({"charged_at": time.time()}),
                        )
                        return "charged"
            except Exception:
                logger.exception("shop ops charge: PG failed")
                # Не переключаемся на JSON здесь — баланс в PG;
                # вызывающий код трактует 'error' как отказ.
                return "error"
        # JSON-фолбэк: отдельные шаги (без PG-транзакции)
        op = self._json_ops.get(op_id)
        if op is None:
            return "missing"
        if op["status"] != "reserved":
            return "exists"
        from storage.user_balance_store import user_balance_store
        try:
            await user_balance_store.subtract_balance(
                guild_id, user_id, amount
            )
        except ValueError:
            op["status"] = "failed"
            self._save_json()
            return "insufficient"
        except Exception:
            return "error"
        op["status"] = "charged"
        op["payload"]["charged_at"] = time.time()
        self._save_json()
        return "charged"

    # -------------------------------------------------------------- refund

    async def refund(
        self,
        op_id: str,
        guild_id: int,
        user_id: int,
        amount: int,
    ) -> dict[str, int]:
        """Идемпотентный возврат. Возвращает {credited, pending}.

        credited — фактически зачислено сейчас (может быть 0 при
        повторном вызове — грант уже существует); pending — сумма,
        не влезшая из-за MAX_BALANCE (догоняется позже).
        """
        if amount <= 0:
            return {"credited": 0, "pending": 0}
        from storage.user_balance_store import user_balance_store
        try:
            credited = await user_balance_store.add_balance_once(
                guild_id, user_id, amount, f"shopref:{op_id}"
            )
        except Exception:
            logger.exception("shop refund: add_balance_once failed")
            return {"credited": 0, "pending": amount}
        pending = max(0, amount - int(credited or 0))
        return {"credited": int(credited or 0), "pending": pending}

    async def settle_pending_refund(self, op: dict[str, Any]) -> int:
        """Досчитать refund_pending малыми идемпотентными грантами.

        Сначала повторно опрашивает базовый грант `shopref:{op_id}`:
        add_balance_once идемпотентен и вернёт зафиксированную сумму
        даже если первый запрос выполнился, а ответ потерялся —
        это исключает двойное начисление.

        Возвращает оставшийся pending. Каждая попытка досчёта —
        новый ключ `shoprefp:{op_id}:{n}`, повторный вызов безопасен.
        """
        payload = op.get("payload") or {}
        pending = int(payload.get("refund_pending") or 0)
        if pending <= 0:
            return 0
        from storage.user_balance_store import user_balance_store

        # 1) Базовый грант: вернёт recorded amount (идемпотентно).
        #    Если он ещё не выполнялся — выполнит сейчас.
        try:
            base_credited = int(await user_balance_store.add_balance_once(
                op["guild_id"], op["user_id"], op["price"],
                f"shopref:{op['op_id']}",
            ) or 0)
        except Exception:
            logger.exception("settle: base refund probe failed")
            return pending
        # Сколько базовый грант не покрыл (cap MAX_BALANCE и т.п.)
        uncovered = max(0, op["price"] - base_credited)
        # Уже досчитано через shoprefp-ключи (refund_settled —
        # отдельный счётчик, refund_credited хранит базовый грант)
        settled = int(payload.get("refund_settled") or 0)
        pending = max(0, uncovered - settled)
        if pending <= 0:
            await self.set_status(
                op["op_id"], op["status"],
                payload={"refund_pending": 0},
            )
            return 0

        attempts = int(payload.get("refund_attempts") or 0)
        settled_total = settled
        while pending > 0 and attempts < 10:
            attempts += 1
            try:
                credited = await user_balance_store.add_balance_once(
                    op["guild_id"], op["user_id"], pending,
                    f"shoprefp:{op['op_id']}:{attempts}",
                )
            except Exception:
                logger.exception("settle pending refund failed")
                break
            credited = int(credited or 0)
            pending -= credited
            settled_total += credited
            if credited <= 0:
                break  # баланс всё ещё на cap — пробуем позже
        await self.set_status(
            op["op_id"], op["status"],
            payload={
                "refund_pending": pending,
                "refund_settled": settled_total,
                "refund_attempts": attempts,
            },
        )
        return pending

    # -------------------------------------------------------------- queries

    async def pending(self) -> list[dict[str, Any]]:
        if self._use_db:
            try:
                pool = await self._pool()
                async with pool.acquire() as conn:
                    await self._ensure_ddl(conn)
                    rows = await conn.fetch(
                        "SELECT * FROM shop_operations "
                        "WHERE status = ANY($1::text[]) "
                        "ORDER BY created_at",
                        list(PENDING_STATUSES),
                    )
                    return [self._row_to_op(r) for r in rows]
            except Exception:
                logger.exception("shop ops pending: PG failed")
                self._use_db = False
        return [
            dict(op) for op in self._json_ops.values()
            if op.get("status") in PENDING_STATUSES
        ]

    async def recent(
        self, guild_id: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if self._use_db:
            try:
                pool = await self._pool()
                async with pool.acquire() as conn:
                    await self._ensure_ddl(conn)
                    if guild_id is None:
                        rows = await conn.fetch(
                            "SELECT * FROM shop_operations "
                            "ORDER BY created_at DESC LIMIT $1", limit
                        )
                    else:
                        rows = await conn.fetch(
                            "SELECT * FROM shop_operations "
                            "WHERE guild_id=$1 "
                            "ORDER BY created_at DESC LIMIT $2",
                            guild_id, limit,
                        )
                    return [self._row_to_op(r) for r in rows]
            except Exception:
                logger.exception("shop ops recent: PG failed")
        ops = sorted(
            self._json_ops.values(),
            key=lambda o: o.get("created_at", 0), reverse=True,
        )
        if guild_id is not None:
            ops = [o for o in ops if o.get("guild_id") == guild_id]
        return [dict(o) for o in ops[:limit]]


purchase_ops_store = ShopPurchaseOpsStore()
