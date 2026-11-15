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
        rep_amount: int = 0,
    ) -> dict[str, Any]:
        """Создать операцию reserved. Идемпотентно по op_id.

        Операции с REP (rep_amount>0) НИКОГДА не уходят в JSON —
        при недоступности PG бросается RuntimeError: лучше отказать
        в покупке, чем потерять финансовую операцию из вида recovery.
        """
        payload = dict(payload or {})
        if rep_amount:
            payload["rep_amount"] = int(rep_amount)
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
                if rep_amount:
                    logger.error(
                        "shop ops create: PG failed for REP op — "
                        "JSON fallback refused"
                    )
                    raise RuntimeError(
                        "PostgreSQL unavailable for REP operation"
                    )
                logger.exception("shop ops create: PG failed, json fallback")
        if rep_amount:
            # _use_db=False (legacy degrade) — REP всё равно только PG.
            raise RuntimeError("PostgreSQL required for REP operation")
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
        rep_amount: int = 0,
    ) -> str:
        """Атомарно: списать баланс(ы) + перевести op reserved→charged.

        При rep_amount>0 монеты и REP списываются в ОДНОЙ транзакции;
        недостаток любой валюты — ничего не списывается.

        Возвращает:
          "charged"    — деньги списаны, операция charged
          "insufficient" — не хватило монет (op → failed)
          "insufficient_rep" — не хватило REP (op → failed)
          "exists"     — op уже charged/done (повторная обработка)
          "missing"    — op не найден
          "error"      — ошибка БД
        """
        if amount < 0 or rep_amount < 0:
            return "error"
        if self._use_db:
            try:
                from storage.user_balance_store import (
                    user_balance_store,
                )
                from storage.rep_balance_store import rep_balance_store
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
                        # REP: миграция/блокировка до проверок —
                        # обе проверки до любых UPDATE.
                        rep_ok = True
                        if rep_amount:
                            rep_balance = (
                                await rep_balance_store._migrate_and_lock(
                                    conn, guild_id, user_id
                                )
                            )
                            rep_ok = rep_balance >= rep_amount
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
                        if not rep_ok:
                            await conn.execute(
                                """
                                UPDATE shop_operations
                                SET status='failed', updated_at=now(),
                                    payload = payload ||
                                        '{"reason":"insufficient_rep"}'::jsonb
                                WHERE op_id=$1
                                """,
                                op_id,
                            )
                            return "insufficient_rep"
                        await conn.execute(
                            "UPDATE user_balance SET balance=$1 "
                            "WHERE guild_id=$2 AND user_id=$3",
                            balance - amount, guild_id, user_id,
                        )
                        if rep_amount:
                            await rep_balance_store.spend_in_tx(
                                conn, guild_id, user_id, rep_amount
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
                            json.dumps({
                                "charged_at": time.time(),
                                "rep_amount": rep_amount,
                            }),
                        )
                        return "charged"
            except Exception:
                logger.exception("shop ops charge: PG failed")
                # Не переключаемся на JSON глобально — PG-операции
                # должны оставаться видимыми recovery. JSON-путь
                # ниже сработает только для операций, реально
                # созданных в JSON-режиме (coin-only).
        # JSON-фолбэк: REP-операции без PG не выполняются —
        # финансовые операции с REP только в PostgreSQL.
        if rep_amount:
            logger.error(
                "shop ops charge: PG unavailable, rep_amount=%s rejected",
                rep_amount,
            )
            return "error"
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
        rep_amount: int | None = None,
    ) -> dict[str, int]:
        """Идемпотентный возврат монет и REP. Возвращает словарь:

            credited      — монеты, зачисленные сейчас/зафиксированные
            pending       — монеты, не влезшие из-за MAX_BALANCE
            rep_credited  — REP, возвращённые (идемпотентно)
            rep_pending   — REP, не возвращённые из-за сбоя БД

        rep_amount берётся из payload операции, если не передан явно.
        """
        res: dict[str, int] = {
            "credited": 0, "pending": 0,
            "rep_credited": 0, "rep_pending": 0,
        }
        if rep_amount is None:
            op = await self.get(op_id)
            rep_amount = int(
                ((op or {}).get("payload") or {}).get("rep_amount") or 0
            )
        rep_amount = int(rep_amount or 0)

        if amount > 0:
            from storage.user_balance_store import user_balance_store
            try:
                credited = await user_balance_store.add_balance_once(
                    guild_id, user_id, amount, f"shopref:{op_id}"
                )
            except Exception:
                logger.exception("shop refund: add_balance_once failed")
                res["pending"] = amount
            else:
                res["credited"] = int(credited or 0)
                res["pending"] = max(0, amount - res["credited"])

        if rep_amount > 0:
            from storage.rep_balance_store import rep_balance_store
            try:
                rep_credited = await rep_balance_store.add_rep_once(
                    guild_id, user_id, rep_amount, f"shoprefrep:{op_id}"
                )
            except Exception:
                logger.exception("shop refund: rep add_rep_once failed")
                res["rep_pending"] = rep_amount
            else:
                res["rep_credited"] = int(rep_credited or 0)
                # REP не имеет cap — pending только при полном сбое;
                # recorded amount < rep_amount возможно лишь при
                # повторном вызове после частичного успеха, что
                # для атомарного гранта невозможно → pending=0.
                res["rep_pending"] = max(
                    0, rep_amount - res["rep_credited"]
                )
        return res

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
        rep_pending = int(payload.get("rep_pending") or 0)
        rep_amount = int(payload.get("rep_amount") or 0)
        # REP-досчёт: cap отсутствует, pending возможен только после
        # сбоя БД — повторный идемпотентный грант разрешает его
        # (вернёт recorded amount или выполнит начисление). Пробуем
        # всегда при rep_amount>0: settle вызывается только для
        # refund-контекста, а грант идемпотентен.
        if rep_amount > 0:
            from storage.rep_balance_store import rep_balance_store
            try:
                rep_credited = await rep_balance_store.add_rep_once(
                    op["guild_id"], op["user_id"], rep_amount,
                    f"shoprefrep:{op['op_id']}",
                )
                # Грант атомарен: recorded amount == rep_amount
                # означает, что REP возвращены (сейчас или ранее —
                # ответ мог быть потерян). Иначе сбой не считается
                # погашенным.
                rep_pending = (
                    0 if int(rep_credited or 0) >= rep_amount
                    else rep_amount - int(rep_credited or 0)
                )
            except Exception:
                logger.exception("settle: rep refund probe failed")
        if pending <= 0 and rep_pending <= 0:
            await self.set_status(
                op["op_id"], op["status"],
                payload={"refund_pending": 0, "rep_pending": 0},
            )
            return 0
        if pending <= 0:
            # Монеты возвращены, REP ещё нет — фиксируем и выходим.
            await self.set_status(
                op["op_id"], op["status"],
                payload={"refund_pending": 0, "rep_pending": rep_pending},
            )
            return rep_pending
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
            return pending + rep_pending
        # Сколько базовый грант не покрыл (cap MAX_BALANCE и т.п.)
        uncovered = max(0, op["price"] - base_credited)
        # Уже досчитано через shoprefp-ключи (refund_settled —
        # отдельный счётчик, refund_credited хранит базовый грант)
        settled = int(payload.get("refund_settled") or 0)
        pending = max(0, uncovered - settled)
        if pending <= 0:
            await self.set_status(
                op["op_id"], op["status"],
                payload={"refund_pending": 0, "rep_pending": rep_pending},
            )
            return rep_pending

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
                "rep_pending": rep_pending,
            },
        )
        return pending + rep_pending

    async def with_refund_debt(
        self, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Операции с непогашенной задолженностью по возврату.

        Учитывает ОБЕ валюты: refund_pending (монеты, MAX_BALANCE)
        и rep_pending (REP, сбой БД). Пагинация через offset —
        вызывающий код обязан дочитать все страницы.
        """
        if self._use_db:
            try:
                pool = await self._pool()
                async with pool.acquire() as conn:
                    await self._ensure_ddl(conn)
                    rows = await conn.fetch(
                        """
                        SELECT * FROM shop_operations
                        WHERE COALESCE((payload->>'refund_pending')::int, 0) > 0
                           OR COALESCE((payload->>'rep_pending')::int, 0) > 0
                        ORDER BY created_at
                        LIMIT $1 OFFSET $2
                        """,
                        limit, offset,
                    )
                    return [self._row_to_op(r) for r in rows]
            except Exception:
                logger.exception("shop ops debt query: PG failed")
                return []
        return [
            dict(op) for op in self._json_ops.values()
            if int((op.get("payload") or {}).get("refund_pending") or 0) > 0
            or int((op.get("payload") or {}).get("rep_pending") or 0) > 0
        ][offset:offset + limit]

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
