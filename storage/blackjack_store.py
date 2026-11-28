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
    op_id TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS op_id TEXT;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS delivery_state TEXT NOT NULL DEFAULT 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS blackjack_one_active
    ON blackjack_rounds (guild_id, user_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS blackjack_op_id
    ON blackjack_rounds (op_id) WHERE op_id IS NOT NULL;
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


class StaleVersion(Exception):
    """Кнопка привязана к устаревшей версии состояния раздачи."""


class _OpIdConflict(Exception):
    """Внутренний: unique(op_id) сработал внутри tx → откат + retry."""


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
    op_id: str | None = None
    delivery_state: str = "pending"  # pending|sending|delivered|unknown|failed
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
        # .get — устойчивость к строкам, созданным до ALTER-миграции
        op_id=row.get("op_id"),
        delivery_state=row.get("delivery_state") or "pending",
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
    async def _credit_in_tx(conn, guild_id: int, user_id: int, amount: int,
                            *, cap_exempt: bool = False) -> int:
        """Начисление с decay/cap — семантика user_balance_store.add_balance,
        но внутри уже открытой транзакции caller'а.

        ``cap_exempt=True`` — для settlement/refund blackjack: ставка уже
        списана, обещанная выплата обязана исполняться полностью (ledger
        и UI показывают gross). Иначе cap может «съесть» часть выплаты —
        расхождение обещанного и фактического."""
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
        cap = _max_balance() if not cap_exempt else 10**15
        add = min(amount, max(0, cap - balance))
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
        op_id: str | None = None,
    ) -> tuple[str, bool]:
        """Атомарно: условное списание ставки + INSERT round + ledger 'stake'.

        ``op_id`` — идемпотентность подтверждения ставки: повторный вызов
        с тем же op_id возвращает (существующий round_id, False) БЕЗ
        повторного списания — покрывает двойной клик «Подтвердить» и
        мгновенное завершение natural-blackjack (partial unique index
        активных раздач такой сценарий не ловит).

        Возвращает (round_id, created). Бросает InsufficientFunds /
        ActiveGameExists.
        """
        if stake < 0:
            raise ValueError("stake must be >= 0")
        round_id = uuid.uuid4().hex
        deadline = datetime.now(UTC) + timedelta(
            seconds=TURN_TIMEOUT_SECONDS
        )
        state = serialize_round(game)

        pool = await self._pool()
        try:
            async with pool.acquire() as conn, conn.transaction():
                await self._ensure_schema(conn)
                if op_id is not None:
                    existing = await conn.fetchrow(
                        "SELECT round_id FROM blackjack_rounds WHERE op_id=$1",
                        op_id,
                    )
                    if existing is not None:
                        return existing["round_id"], False
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
                            outcome, reason, status, deadline, settled_at,
                            op_id, delivery_state)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
                        round_id, guild_id, user_id, channel_id, stake,
                        game.rules_version,
                        json.dumps(state["deck"]),
                        json.dumps(state["player"]),
                        json.dumps(state["dealer"]),
                        state["phase"], outcome, reason,
                        "settled" if done else "active",
                        None if done else deadline,
                        datetime.now(UTC) if done else None,
                        op_id, "pending",
                    )
                except Exception as exc:
                    msg = str(exc).lower()
                    if op_id is not None and "blackjack_op_id" in msg:
                        # гонка двух подтверждений: откат (включая debit)
                        # и идемпотентный повтор снаружи транзакции
                        raise _OpIdConflict(op_id) from exc
                    # partial unique index → активная игра уже есть
                    if "blackjack_one_active" in msg or "unique" in msg:
                        raise ActiveGameExists() from exc
                    raise
        except _OpIdConflict:
            resolved = await self._resolve_op_conflict(op_id)  # type: ignore[arg-type]
            if resolved is not None:
                return resolved
            raise
        return round_id, True

    # ---------------- чтение ----------------

    async def get_by_op_id(self, op_id: str) -> StoredRound | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM blackjack_rounds WHERE op_id=$1", op_id
            )
            return _row_to_round(row) if row else None

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

    async def apply_action(
        self,
        round_id: str,
        action: str,
        expected_version: int | None = None,
    ) -> StoredRound:
        """'hit' | 'stand' | 'auto_stand'. Row-lock + version CAS.

        ``expected_version`` — версия, которую видел игрок на кнопке.
        Расхождение → StaleVersion, состояние не меняется: исключает
        гонки Hit/Hit, Hit/Stand, двойные клики по устаревшей кнопке.

        Deadline проверяется ВНУТРИ транзакции: если время хода истекло,
        любой запрос игрока превращается в auto_stand (раздача
        завершается детерминированно, без гонки со sweeper'ом).

        Возвращает обновлённый StoredRound. Бросает RoundNotActive /
        RoundNotFound / StaleVersion. При завершении раздачи settlement
        выполняется в той же транзакции.
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
            if (
                expected_version is not None
                and int(row["version"]) != int(expected_version)
            ):
                raise StaleVersion(round_id)
            expired = False
            dl = row["deadline"]
            if dl is not None:
                if dl.tzinfo is None:
                    dl = dl.replace(tzinfo=UTC)
                expired = dl <= datetime.now(UTC)
            if action == "auto_stand" and not expired:
                # sweeper вызван досрочно — deadline ещё не наступил
                raise RoundNotActive(round_id)
            stored = _row_to_round(row)
            if expired or action == "auto_stand":
                # ход после истечения deadline → авто-Stand
                new_game = stored.game.stand()
            elif action == "hit":
                new_game = stored.game.hit()
            else:
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
            # cap_exempt: обещанная выплата исполняется полностью —
            # stake уже списан, ledger и UI показывают gross.
            await self._credit_in_tx(
                conn, guild_id, user_id, gross, cap_exempt=True
            )
        return gross

    # ---------------- служебные ----------------

    async def mark_message(self, round_id: str, message_id: int) -> None:
        """Доставка подтверждена: message_id + delivery_state='delivered'."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE blackjack_rounds
                   SET message_id=$2, delivery_state='delivered',
                       updated_at=now()
                   WHERE round_id=$1 AND message_id IS NULL""",
                round_id, message_id,
            )

    async def mark_delivery(self, round_id: str, state: str) -> None:
        """Фиксация состояния доставки: sending | unknown | failed."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE blackjack_rounds SET delivery_state=$2,
                       updated_at=now()
                   WHERE round_id=$1 AND delivery_state <> 'delivered'""",
                round_id, state,
            )

    async def list_undelivered(
        self, older_than: datetime, limit: int = 50
    ) -> list[StoredRound]:
        """Раздачи без подтверждённой доставки старше ``older_than``:
        message_id IS NULL и delivery_state IN
        (pending, sending, unknown, failed, delivered) —
        'delivered' без message_id = отправка подтверждена, но id
        не записался → тоже кандидат на поиск по маркеру."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM blackjack_rounds
                   WHERE message_id IS NULL
                     AND delivery_state IN
                         ('pending','sending','unknown','failed','delivered')
                     AND created_at < $1
                   ORDER BY created_at LIMIT $2""",
                older_than, limit,
            )
            return [_row_to_round(r) for r in rows]

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
        """Idempotent refund ТОЛЬКО при доказанном отсутствии доставки.

        Условия (все внутри FOR UPDATE):
        - status='active' — settled/void/refunded не трогаем
          (natural-blackjack уже рассчитан — выплата стоит);
        - message_id IS NULL;
        - delivery_state IN ('pending','failed') — HTTP к Discord
          либо не вызывался ('pending' — крах до send, 'failed' —
          reconcile доказал отсутствие сообщения маркером).
          'sending'/'unknown'/'delivered' → False: неоднозначная
          доставка НЕ рефандится автоматически.
        """
        pool = await self._pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM blackjack_rounds WHERE round_id=$1 FOR UPDATE",
                round_id,
            )
            if row is None:
                return False
            if (
                row["status"] != "active"
                or row["message_id"] is not None
                or row["delivery_state"] not in ("pending", "failed")
            ):
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
            await self._credit_in_tx(
                conn, int(row["guild_id"]), int(row["user_id"]), stake,
                cap_exempt=True,
            )
            await conn.execute(
                "UPDATE blackjack_rounds SET status='refunded', settled_at=now(), updated_at=now() WHERE round_id=$1",
                round_id,
            )
            return True


blackjack_store = BlackjackStore()
