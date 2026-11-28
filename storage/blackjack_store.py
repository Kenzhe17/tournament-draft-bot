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
    delivery_attempt TEXT,
    reconcile_attempts INTEGER NOT NULL DEFAULT 0,
    last_reconcile_at TIMESTAMPTZ,
    card_rendered INTEGER NOT NULL DEFAULT -1,
    card_claim INTEGER NOT NULL DEFAULT -1,
    card_closed BOOLEAN NOT NULL DEFAULT FALSE,
    dispute_resolved_at TIMESTAMPTZ,
    dispute_resolved_by BIGINT,
    dispute_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS op_id TEXT;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS delivery_state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS delivery_attempt TEXT;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS reconcile_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS last_reconcile_at TIMESTAMPTZ;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS card_rendered INTEGER NOT NULL DEFAULT -1;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS card_claim INTEGER NOT NULL DEFAULT -1;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS card_closed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS dispute_resolved_at TIMESTAMPTZ;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS dispute_resolved_by BIGINT;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS dispute_note TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS blackjack_one_active
    ON blackjack_rounds (guild_id, user_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS blackjack_op_id
    ON blackjack_rounds (op_id) WHERE op_id IS NOT NULL;
-- Очередь reconcile: только недоставленные финально-необработанные
-- раздачи (refunded/void исключены — они не должны занимать LIMIT).
CREATE INDEX IF NOT EXISTS blackjack_undelivered_idx
    ON blackjack_rounds (last_reconcile_at ASC NULLS FIRST, created_at)
    WHERE message_id IS NULL
      AND status IN ('active','settled')
      AND delivery_state IN
          ('pending','sending','unknown','failed','delivered');
-- Самолечение карточек: claim отстаёт от version → нужен re-render.
CREATE INDEX IF NOT EXISTS blackjack_card_sync_idx
    ON blackjack_rounds (updated_at)
    WHERE message_id IS NOT NULL AND card_claim < version;
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
    delivery_state: str = "pending"
    # pending|sending|delivered|unknown|failed|disputed
    card_rendered: int = -1   # версия состояния, подтверждённо показанная
    card_claim: int = -1      # версия, захваченная для рендера
    card_closed: bool = False  # «Закрыть»: кнопки сняты, результат виден
    delivery_attempt: str | None = None  # attempt_id текущего claim
    reconcile_attempts: int = 0
    last_reconcile_at: datetime | None = None
    dispute_resolved_at: datetime | None = None
    dispute_resolved_by: int | None = None
    dispute_note: str | None = None
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
        card_rendered=(
            int(row["card_rendered"]) if row.get("card_rendered") is not None
            else -1),
        card_claim=(
            int(row["card_claim"]) if row.get("card_claim") is not None
            else -1),
        card_closed=bool(row.get("card_closed", False)),
        delivery_attempt=row.get("delivery_attempt"),
        reconcile_attempts=int(row.get("reconcile_attempts", 0) or 0),
        last_reconcile_at=row.get("last_reconcile_at"),
        dispute_resolved_at=row.get("dispute_resolved_at"),
        dispute_resolved_by=(
            int(row["dispute_resolved_by"])
            if row.get("dispute_resolved_by") is not None else None),
        dispute_note=row.get("dispute_note"),
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
        """Дневной decay — делегирован UserBalanceStore._apply_decay
        (DB marks user_decay_marks: rollback-safe, multi-process safe)."""
        from storage.user_balance_store import user_balance_store
        return await user_balance_store._apply_decay(
            conn, guild_id, user_id, balance)

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
        # user_balance.balance — INTEGER (max 2 147 483 647). cap_exempt
        # гарантирует полную выплату, но не имеет права переполнить
        # колонку даже теоретически (параллельные переводы/начисления
        # во время активной раздачи). Потолок 2e9 недостижим в
        # реальной игре (макс. выплата 20k) — защита от overflow,
        # а не ограничение выигрыша. BIGINT-миграция — отдельным
        # согласованным планом, глобальный MAX_BALANCE не меняется.
        cap = _max_balance() if not cap_exempt else 2_000_000_000
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

    async def claim_delivery(self, round_id: str, attempt_id: str) -> bool:
        """Атомарный CAS-захват права на отправку раздачи.

        Переход delivery_state pending|failed → 'sending' с уникальным
        attempt_id — только ОДИН обработчик (даже в двух процессах)
        получает право вызвать Discord. 'sending'/'unknown'/'delivered'
        не клеймятся: blind resend запрещён — неоднозначная доставка
        разрешается только reconcile по маркеру.

        PG-транзакция НЕ удерживается на время Discord HTTP — claim
        и send разделены; крах между ними → 'sending' до reconcile.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds
                   SET delivery_state='sending', delivery_attempt=$2,
                       updated_at=now()
                   WHERE round_id=$1 AND message_id IS NULL
                     AND delivery_state IN ('pending','failed')
                     AND status IN ('active','settled')
                   RETURNING round_id""",
                round_id, attempt_id,
            )
            return row is not None

    async def mark_message(self, round_id: str, message_id: int) -> None:
        """Доставка подтверждена: message_id + delivery_state='delivered'.

        Содержимое карточки — текущее состояние версии ``version``,
        поэтому рендер-счётчики сразу выставляются в неё: sweeper
        не будет считать только что отправленную карту «устаревшей»."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE blackjack_rounds
                   SET message_id=$2, delivery_state='delivered',
                       card_rendered=version, card_claim=version,
                       updated_at=now()
                   WHERE round_id=$1 AND message_id IS NULL""",
                round_id, message_id,
            )

    async def detach_message(
        self, channel_id: int, message_id: int, except_round_id: str
    ) -> int:
        """Снять владение публичной карточкой с других (завершённых)
        раздач — атомарно, ДО редактирования сообщения новой раздачей.

        После этого устаревшие обработчики старой раздачи не смогут
        легально редактировать карточку (их stored.message_id NULL).
        Возвращает число отсоединённых строк."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """WITH moved AS (
                       UPDATE blackjack_rounds SET message_id=NULL,
                           updated_at=now()
                       WHERE channel_id=$1 AND message_id=$2
                         AND round_id<>$3 AND status='settled'
                       RETURNING round_id)
                   SELECT count(*) AS n FROM moved""",
                channel_id, message_id, except_round_id,
            )
            return int(row["n"]) if row else 0

    async def mark_delivery(
        self, round_id: str, state: str, attempt_id: str | None = None
    ) -> None:
        """Фиксация состояния доставки после claim.

        При ``attempt_id`` — CAS: переход разрешён только из 'sending'
        и только владельцем попытки (защита от устаревшего обработчика).
        Без attempt_id (reconcile) — из любого нефинального состояния,
        но никогда не регрессирует 'delivered'."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            if attempt_id is not None:
                await conn.execute(
                    """UPDATE blackjack_rounds SET delivery_state=$2,
                           updated_at=now()
                       WHERE round_id=$1 AND delivery_state='sending'
                         AND delivery_attempt=$3""",
                    round_id, state, attempt_id,
                )
            else:
                await conn.execute(
                    """UPDATE blackjack_rounds SET delivery_state=$2,
                           updated_at=now()
                       WHERE round_id=$1 AND delivery_state <> 'delivered'""",
                    round_id, state,
                )

    async def note_reconcile_attempt(self, round_id: str) -> int:
        """Отметить проход reconcile: +1 попытка, вернуть новое число."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds
                   SET reconcile_attempts=reconcile_attempts+1,
                       last_reconcile_at=now(), updated_at=now()
                   WHERE round_id=$1 RETURNING reconcile_attempts""",
                round_id,
            )
            return int(row["reconcile_attempts"]) if row else 0

    async def mark_disputed(self, round_id: str) -> None:
        """Неоднозначная доставка после MAX_RECONCILE_ATTEMPTS —
        флаг для администратора (спорный случай). Раздача продолжает
        жить обычным тайм-аутом; refund не выполняется."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE blackjack_rounds SET delivery_state='disputed',
                       updated_at=now()
                   WHERE round_id=$1 AND message_id IS NULL
                     AND delivery_state IN ('sending','unknown','delivered')""",
                round_id,
            )

    # ---------------- сериализация рендера карточки ----------------
    #
    # claim/finish — lock-free CAS по version: только редактор с
    # НАИБОЛЬШЕЙ версией получает право на HTTP-правку. Порядок
    # завершения HTTP-запросов не важен: устаревший claim отсекается
    # до отправки, а свежий вид всегда рендерится из актуального
    # состояния БД. Работает в нескольких процессах (PG CAS).

    async def claim_card_render(self, round_id: str, version: int) -> bool:
        """Захватить право рендера версии ``version`` карточки.

        Атомарно: card_claim < version → card_claim = version.
        True — этот обработчик единственный актуальный рендерер;
        обязан вызвать finish_card_render после Discord-правки.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds SET card_claim=$2, updated_at=now()
                   WHERE round_id=$1 AND card_claim < $2
                   RETURNING round_id""",
                round_id, version,
            )
            return row is not None

    async def finish_card_render(
        self, round_id: str, version: int, ok: bool
    ) -> None:
        """Завершить захваченный рендер.

        ok=True → card_rendered=version (карточка актуальна).
        ok=False → card_claim откатывается к card_rendered: следующий
        sync (sweeper/recovery/действие игрока) повторит попытку —
        финансовое состояние при этом не трогается.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            if ok:
                await conn.execute(
                    """UPDATE blackjack_rounds SET card_rendered=$2,
                           updated_at=now()
                       WHERE round_id=$1 AND card_claim=$2""",
                    round_id, version,
                )
            else:
                await conn.execute(
                    """UPDATE blackjack_rounds
                       SET card_claim=card_rendered, updated_at=now()
                       WHERE round_id=$1 AND card_claim=$2""",
                    round_id, version,
                )

    async def mark_card_closed(self, round_id: str) -> None:
        """«Закрыть»: карточка больше не показывает кнопки.
        Флаг в БД — повторные sync не вернут кнопки."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE blackjack_rounds SET card_closed=TRUE, "
                "updated_at=now() WHERE round_id=$1",
                round_id,
            )

    async def list_needing_sync(self, limit: int = 25) -> list[StoredRound]:
        """Карточки, у которых claim отстал от version — рендер
        не подтверждён (ошибка Discord API, рестарт до edit).
        Самолечение: sweeper периодически догоняет отображение."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM blackjack_rounds
                   WHERE message_id IS NOT NULL
                     AND card_claim < version
                     AND status IN ('active','settled')
                   ORDER BY updated_at ASC
                   LIMIT $1""",
                limit,
            )
            return [_row_to_round(r) for r in rows]

    async def request_card_resync(self, round_id: str) -> bool:
        """Админ: пометить карточку к повторному отображению.
        Сбрасывает render-счётчики — ближайший проход sweeper
        (list_needing_sync) перерисует актуальное состояние."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds
                   SET card_claim=-1, card_rendered=-1, updated_at=now()
                   WHERE round_id=$1 AND message_id IS NOT NULL
                   RETURNING round_id""",
                round_id,
            )
            return row is not None

    # ---------------- спорные раздачи (admin) ----------------

    async def list_disputed(self) -> list[StoredRound]:
        """Неоднозначная доставка, ожидающая решения администратора."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM blackjack_rounds
                   WHERE delivery_state='disputed'
                     AND dispute_resolved_at IS NULL
                   ORDER BY created_at ASC""",
            )
            return [_row_to_round(r) for r in rows]

    async def get_dispute_ledger(self, round_id: str) -> list[dict]:
        """Ledger-операции раздачи для админ-диагностики."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT op, amount, created_at FROM blackjack_ledger
                   WHERE round_id=$1 ORDER BY id""",
                round_id,
            )
            return [dict(r) for r in rows]

    async def recheck_delivery(self, round_id: str) -> bool:
        """Админ: вернуть раздачу в очередь reconcile.

        Сбрасывает счётчик попыток и решение спора — следующий
        проход sweeper снова проверит доставку по маркеру."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds
                   SET delivery_state='unknown', reconcile_attempts=0,
                       last_reconcile_at=NULL,
                       dispute_resolved_at=NULL, dispute_resolved_by=NULL,
                       dispute_note=NULL, updated_at=now()
                   WHERE round_id=$1 AND delivery_state='disputed'
                   RETURNING round_id""",
                round_id,
            )
            return row is not None

    async def resolve_dispute(
        self, round_id: str, actor_id: int, note: str
    ) -> bool:
        """Админ: закрыть спор БЕЗ финансовых изменений."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds
                   SET dispute_resolved_at=now(), dispute_resolved_by=$2,
                       dispute_note=$3, updated_at=now()
                   WHERE round_id=$1 AND delivery_state='disputed'
                     AND dispute_resolved_at IS NULL
                   RETURNING round_id""",
                round_id, actor_id, note[:500],
            )
            return row is not None

    async def compensate_dispute(
        self, round_id: str, actor_id: int, op_id: str,
        amount: int, reason: str,
    ) -> dict:
        """Админ-компенсация: идемпотентная ledger-операция.

        Одна tx: blackjack_ledger op='admin_comp:<op_id>' (UNIQUE →
        повтор невозможен) + cap-exempt credit + отметка решения
        спора с актёром/причиной. Карты, исход и исходная выплата
        НЕ меняются — только дополнительное начисление.
        """
        if amount <= 0:
            raise ValueError("amount must be positive")
        pool = await self._pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM blackjack_rounds WHERE round_id=$1 "
                "FOR UPDATE",
                round_id,
            )
            if row is None:
                raise RoundNotFound(round_id)
            ledger_op = f"admin_comp:{op_id}"
            inserted = await self._ledger(conn, round_id, ledger_op, amount)
            if not inserted:
                return {"status": "duplicate"}
            await self._credit_in_tx(
                conn, int(row["guild_id"]), int(row["user_id"]),
                amount, cap_exempt=True,
            )
            await conn.execute(
                """UPDATE blackjack_rounds
                   SET dispute_resolved_at=now(), dispute_resolved_by=$2,
                       dispute_note=$3, updated_at=now()
                   WHERE round_id=$1""",
                round_id, actor_id,
                f"compensation +{amount}: {reason}"[:500],
            )
            return {"status": "applied", "amount": amount}

    async def list_undelivered(
        self, older_than: datetime, limit: int = 50
    ) -> list[StoredRound]:
        """Раздачи без подтверждённой доставки старше ``older_than``.

        Ротация: сначала ни разу не проверенные (любого возраста),
        затем давно не проверенные — застрявшие 'unknown' не блокируют
        очередь. 'disputed' исключён (админ-диагностика)."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM blackjack_rounds
                   WHERE message_id IS NULL
                     AND status IN ('active','settled')
                     AND delivery_state IN
                         ('pending','sending','unknown','failed','delivered')
                     AND created_at < $1
                   ORDER BY last_reconcile_at ASC NULLS FIRST,
                            created_at ASC
                   LIMIT $2""",
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
                     AND deadline IS NOT NULL AND deadline < $1
                     -- гарантированно недоставленные раздачи не
                     -- рассчитываем как игру — их закрывает refund
                     AND NOT (message_id IS NULL
                              AND delivery_state IN ('pending','failed'))""",
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
        - delivery_state IN ('pending','failed') — отправка
          доказуемо не состоялась: 'pending' — крах до send,
          'failed' — pre-send сбой до вызова Discord. 'sending'/
          'unknown'/'delivered'/'disputed' → False: неоднозначная
          или подтверждённая доставка НЕ рефандится автоматически.
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
