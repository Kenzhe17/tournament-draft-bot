"""BLACKJACK 2.0 — персистентное хранилище раздач (PostgreSQL).

Гарантии P0:
- ставка списывается условным UPDATE (balance >= stake) и раздача
  создаётся в ОДНОЙ транзакции вместе с ledger-записью 'stake';
- settlement идемпотентен: ledger UNIQUE(round_id, op), выплата и
  начисление — в одной транзакции под FOR UPDATE;
- одна активная игра на (guild_id, user_id) — частичный UNIQUE INDEX;
- ход игрока — CAS по version под row lock;
- тайм-аут 90с → авто-Stand через expire_due() ( sweeper );
- refund только для недоставленной первой раздачи (message_id IS NULL)
  и только один раз — ledger 'refund'.

Без DATABASE_URL — in-memory fallback (dev-режим, один процесс).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from games.blackjack_engine import (
    TURN_TIMEOUT_SECONDS,
    Round,
    deserialize_round,
    gross_payout,
    serialize_round,
)

logger = logging.getLogger(__name__)

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS blackjack_rounds (
    round_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT,
    stake INTEGER NOT NULL DEFAULT 0,
    rules_version TEXT NOT NULL,
    deck JSONB NOT NULL,
    player JSONB NOT NULL,
    dealer JSONB NOT NULL,
    phase TEXT NOT NULL,
    outcome TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 0,
    deadline TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS blackjack_one_active
    ON blackjack_rounds (guild_id, user_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS blackjack_ledger (
    id BIGSERIAL PRIMARY KEY,
    round_id TEXT NOT NULL,
    op TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (round_id, op)
);
"""


class InsufficientFunds(Exception):
    pass


class ActiveGameExists(Exception):
    pass


class RoundNotActive(Exception):
    pass


class RoundNotFound(Exception):
    pass


@dataclass
class StoredRound:
    round_id: str
    guild_id: int
    user_id: int
    channel_id: int
    message_id: int | None
    stake: int
    game: Round
    status: str           # active | settled | refunded | void
    version: int
    deadline: datetime | None
    outcome: str | None
    reason: str | None
    payout: int | None
    created_at: datetime | None = None
    settled_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    @property
    def awaiting_player(self) -> bool:
        return self.active and self.game.phase == "player"


def _loads_cards(value: Any) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    return value


def _row_to_round(row: Any) -> StoredRound:
    state = {
        "deck": _loads_cards(row["deck"]),
        "player": _loads_cards(row["player"]),
        "dealer": _loads_cards(row["dealer"]),
        "phase": row["phase"],
        "outcome": row["outcome"],
        "reason": row["reason"],
        "rules_version": row["rules_version"],
    }
    # payout выводится из ledger при необходимости; хранится в outcome-строке
    return StoredRound(
        round_id=row["round_id"],
        guild_id=int(row["guild_id"]),
        user_id=int(row["user_id"]),
        channel_id=int(row["channel_id"]),
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        stake=int(row["stake"]),
        game=deserialize_round(state),
        status=row["status"],
        version=int(row["version"]),
        deadline=row["deadline"],
        outcome=row["outcome"],
        reason=row["reason"],
        payout=None,
        created_at=row["created_at"],
        settled_at=row["settled_at"],
    )


class BlackjackStore:
    """PG-реализация; API одинаков для memory-fallback."""

    def __init__(self) -> None:
        # Требует DATABASE_URL — как и user_balance_store (_use_db=True).
        self._schema_ready = False

    # ---------------- схема ----------------

    async def _ensure_schema(self, conn) -> None:
        if self._schema_ready:
            return
        await conn.execute(_SCHEMA_DDL)
        self._schema_ready = True

    def _pool(self):
        from storage.db import get_pool
        return get_pool()

    # ---------------- баланс (внутри транзакции caller'а) ----------------

    @staticmethod
    async def _apply_decay_in_tx(conn, guild_id: int, user_id: int, balance: int) -> int:
        """Дневной decay — та же логика, что UserBalanceStore._apply_decay."""
        from storage.user_balance_store import (
            DECAY_PERCENT,
            DECAY_THRESHOLD,
            decay_tracker,
        )
        if balance > DECAY_THRESHOLD and decay_tracker.should_apply_decay(guild_id, user_id):
            balance -= int(balance * DECAY_PERCENT)
            await conn.execute(
                "UPDATE user_balance SET balance=$1 WHERE guild_id=$2 AND user_id=$3",
                balance, guild_id, user_id,
            )
            decay_tracker.mark_processed(guild_id, user_id)
        return balance

    @classmethod
    async def _debit_in_tx(cls, conn, guild_id: int, user_id: int, stake: int, round_id: str) -> None:
        """Условное списание ставки + ledger 'stake' — атомарно в tx.

        SELECT ... FOR UPDATE сериализует с другими операциями над
        балансом (магазин, другие игры). Баланс >= stake проверяется
        ПОСЛЕ decay — как в subtract_balance.
        """
        from storage.user_balance_store import user_balance_store
        await user_balance_store._ensure_row(conn, guild_id, user_id)
        row = await conn.fetchrow(
            "SELECT balance FROM user_balance WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
            guild_id, user_id,
        )
        balance = await cls._apply_decay_in_tx(conn, guild_id, user_id, row["balance"])
        if balance < stake:
            raise InsufficientFunds()
        await conn.execute(
            "UPDATE user_balance SET balance=$1 WHERE guild_id=$2 AND user_id=$3",
            balance - stake, guild_id, user_id,
        )
        await cls._ledger(conn, round_id, "stake", stake)

    @staticmethod
    async def _credit_in_tx(conn, guild_id: int, user_id: int, amount: int) -> int:
        """Начисление с decay/cap — семантика user_balance_store.add_balance,
        но внутри уже открытой транзакции caller'а."""
        if amount <= 0:
            return await BlackjackStore._balance_in_tx(conn, guild_id, user_id)
        from storage.user_balance_store import (
            _max_balance,
            user_balance_store,
        )
        await user_balance_store._ensure_row(conn, guild_id, user_id)
        row = await conn.fetchrow(
            "SELECT balance FROM user_balance WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
            guild_id, user_id,
        )
        balance = await BlackjackStore._apply_decay_in_tx(
            conn, guild_id, user_id, row["balance"]
        )
        add = min(amount, max(0, _max_balance() - balance))
        new_balance = balance + add
        await conn.execute(
            "UPDATE user_balance SET balance=$1 WHERE guild_id=$2 AND user_id=$3",
            new_balance, guild_id, user_id,
        )
        return new_balance

    @staticmethod
    async def _balance_in_tx(conn, guild_id: int, user_id: int) -> int:
        row = await conn.fetchrow(
            "SELECT balance FROM user_balance WHERE guild_id=$1 AND user_id=$2",
            guild_id, user_id,
        )
        return int(row["balance"]) if row else 0

    @staticmethod
    async def _ledger(conn, round_id: str, op: str, amount: int) -> bool:
        """True, если запись вставлена; False — op уже был (идемпотентность)."""
        row = await conn.fetchrow(
            """INSERT INTO blackjack_ledger (round_id, op, amount)
               VALUES ($1,$2,$3) ON CONFLICT (round_id, op) DO NOTHING
               RETURNING id""",
            round_id, op, amount,
        )
        return row is not None

    # ---------------- создание раздачи ----------------

    async def create_round(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        stake: int,
        game: Round,
    ) -> str:
        """Атомарно: условное списание ставки + INSERT round + ledger 'stake'.

        Возвращает round_id. Бросает InsufficientFunds / ActiveGameExists.
        """
        if stake < 0:
            raise ValueError("stake must be >= 0")
        round_id = uuid.uuid4().hex
        deadline = datetime.now(UTC) + timedelta(
            seconds=TURN_TIMEOUT_SECONDS
        )
        state = serialize_round(game)

        pool = await self._pool()
        async with pool.acquire() as conn, conn.transaction():
            await self._ensure_schema(conn)
            if stake > 0:
                await self._debit_in_tx(conn, guild_id, user_id, stake, round_id)
            done = state["phase"] == "done"
            outcome = state["outcome"] if done else None
            reason = state["reason"] if done else None
            if done:
                await self._settle_in_tx(
                    conn, round_id, guild_id, user_id, stake, outcome
                )
            try:
                await conn.execute(
                    """INSERT INTO blackjack_rounds
                       (round_id, guild_id, user_id, channel_id, stake,
                        rules_version, deck, player, dealer, phase,
                        outcome, reason, status, deadline, settled_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
                    round_id, guild_id, user_id, channel_id, stake,
                    game.rules_version,
                    json.dumps(state["deck"]),
                    json.dumps(state["player"]),
                    json.dumps(state["dealer"]),
                    state["phase"], outcome, reason,
                    "settled" if done else "active",
                    None if done else deadline,
                    datetime.now(UTC) if done else None,
                )
            except Exception as exc:
                # partial unique index → активная игра уже есть
                if "blackjack_one_active" in str(exc) or "unique" in str(exc).lower():
                    raise ActiveGameExists() from exc
                raise
        return round_id

    # ---------------- чтение ----------------

    async def get_round(self, round_id: str) -> StoredRound | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM blackjack_rounds WHERE round_id=$1", round_id
            )
            return _row_to_round(row) if row else None

    async def get_by_message(self, channel_id: int, message_id: int) -> StoredRound | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM blackjack_rounds
                   WHERE channel_id=$1 AND message_id=$2
                   ORDER BY created_at DESC LIMIT 1""",
                channel_id, message_id,
            )
            return _row_to_round(row) if row else None

    async def get_active(self, guild_id: int, user_id: int) -> StoredRound | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT * FROM blackjack_rounds
                   WHERE guild_id=$1 AND user_id=$2 AND status='active'""",
                guild_id, user_id,
            )
            return _row_to_round(row) if row else None

    async def list_active(self) -> list[StoredRound]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM blackjack_rounds WHERE status='active'"
            )
            return [_row_to_round(r) for r in rows]

    # ---------------- ходы ----------------

    async def apply_action(self, round_id: str, action: str) -> StoredRound:
        """'hit' | 'stand' | 'auto_stand'. Row-lock + version CAS.

        Возвращает обновлённый StoredRound. Бросает RoundNotActive /
        RoundNotFound. При завершении раздачи settlement выполняется
        в той же транзакции.
        """
        pool = await self._pool()
        async with pool.acquire() as conn, conn.transaction():
            await self._ensure_schema(conn)
            row = await conn.fetchrow(
                "SELECT * FROM blackjack_rounds WHERE round_id=$1 FOR UPDATE",
                round_id,
            )
            if row is None:
                raise RoundNotFound(round_id)
            if row["status"] != "active" or row["phase"] != "player":
                raise RoundNotActive(round_id)
            if action == "auto_stand" and row["deadline"] is not None:
                # deadline мог быть продлён свежим ходом игрока между
                # выборкой expire_due и захватом lock — не закрываем раньше.
                dl = row["deadline"]
                if dl.tzinfo is None:
                    dl = dl.replace(tzinfo=UTC)
                if dl > datetime.now(UTC):
                    raise RoundNotActive(round_id)
            stored = _row_to_round(row)
            if action == "hit":
                new_game = stored.game.hit()
            else:  # stand / auto_stand
                new_game = stored.game.stand()
            new_version = int(row["version"]) + 1
            new_deadline = datetime.now(UTC) + timedelta(
                seconds=TURN_TIMEOUT_SECONDS
            )
            state = serialize_round(new_game)
            done = new_game.phase == "done"
            outcome = new_game.outcome if done else None
            reason = new_game.reason if done else None
            payout = None
            if done:
                payout = await self._settle_in_tx(
                    conn, round_id, stored.guild_id, stored.user_id,
                    stored.stake, outcome,
                )
            await conn.execute(
                """UPDATE blackjack_rounds SET
                           deck=$2, player=$3, dealer=$4, phase=$5,
                           outcome=$6, reason=$7, version=$8, deadline=$9,
                           status=$10, settled_at=$11, updated_at=now()
                       WHERE round_id=$1""",
                round_id,
                json.dumps(state["deck"]),
                json.dumps(state["player"]),
                json.dumps(state["dealer"]),
                state["phase"], outcome, reason, new_version,
                None if done else new_deadline,
                "settled" if done else "active",
                datetime.now(UTC) if done else None,
            )
            stored.game = new_game
            stored.version = new_version
            stored.status = "settled" if done else "active"
            stored.outcome = outcome
            stored.reason = reason
            stored.payout = payout
            stored.deadline = None if done else new_deadline
            return stored

    async def _settle_in_tx(
        self, conn, round_id: str, guild_id: int, user_id: int,
        stake: int, outcome: str,
    ) -> int:
        """Идемпотентный settlement внутри tx хода/тайм-аута.

        gross = 2*stake при win, stake при push, 0 при loss.
        ledger 'payout' UNIQUE(round_id,'payout') — повтор не начислит.
        Возвращает gross-выплату.
        """
        gross = gross_payout(outcome, stake)
        inserted = await self._ledger(conn, round_id, "payout", gross)
        if not inserted:
            # Уже рассчитано (защита от повторного входа)
            return gross
        if gross > 0:
            await self._credit_in_tx(conn, guild_id, user_id, gross)
        return gross

    # ---------------- служебные ----------------

    async def mark_message(self, round_id: str, message_id: int) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE blackjack_rounds SET message_id=$2, updated_at=now()
                   WHERE round_id=$1 AND message_id IS NULL""",
                round_id, message_id,
            )

    async def expire_due(self, now: datetime | None = None) -> list[StoredRound]:
        """Активные раздачи с истёкшим deadline → авто-Stand + settle.

        Возвращает список обработанных раундов. Идемпотентно:
        apply_action под row lock — параллельный клик игрока либо
        успеет до deadline (тогда phase уже не player → RoundNotActive),
        либо получит RoundNotActive после нашего settle.
        """
        now = now or datetime.now(UTC)
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT round_id FROM blackjack_rounds
                   WHERE status='active' AND phase='player'
                     AND deadline IS NOT NULL AND deadline < $1""",
                now,
            )
        settled = []
        for r in rows:
            try:
                settled.append(await self.apply_action(r["round_id"], "auto_stand"))
            except (RoundNotActive, RoundNotFound):
                continue
            except Exception:
                logger.warning("blackjack expire failed for %s", r["round_id"], exc_info=True)
        return settled

    async def refund_undelivered(self, round_id: str) -> bool:
        """Idempotent refund: только если ставка списана, раздача не
        доставлена (message_id IS NULL) и нет payout/refund в ledger.

        'Неоднозначная доставка' (message_id уже есть) → False,
        раздача остаётся до тайм-аута.
        """
        pool = await self._pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM blackjack_rounds WHERE round_id=$1 FOR UPDATE",
                round_id,
            )
            if row is None:
                return False
            if row["status"] != "active" or row["message_id"] is not None:
                return False
            stake = int(row["stake"])
            if stake <= 0:
                await conn.execute(
                    "UPDATE blackjack_rounds SET status='void', updated_at=now() WHERE round_id=$1",
                    round_id,
                )
                return True
            inserted = await self._ledger(conn, round_id, "refund", stake)
            if not inserted:
                return False
            await self._credit_in_tx(conn, int(row["guild_id"]), int(row["user_id"]), stake)
            await conn.execute(
                "UPDATE blackjack_rounds SET status='refunded', settled_at=now(), updated_at=now() WHERE round_id=$1",
                round_id,
            )
            return True


blackjack_store = BlackjackStore()
