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
    BET_MAX,
    TURN_TIMEOUT_SECONDS,
    Round,
    deserialize_round,
    gross_payout,
    serialize_round,
)

logger = logging.getLogger(__name__)

# Lease активной render-операции: дольше — зависший op считается
# мёртвым и может быть перехвачен (takeover). HTTP-рендер карточки
# укладывается в секунды; 30с — с запасом на Discord-очередь.
RENDER_LEASE_SECONDS = 30

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
-- Single-writer рендер карточки: активная операция (op id + время),
-- lease-экспайр для восстановления после краха, отложенная
-- ре-верификация после takeover (зомби-HTTP мог завершиться позже).
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS card_render_op TEXT;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS card_render_started_at TIMESTAMPTZ;
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS card_verify_after TIMESTAMPTZ;
-- Не более одной админ-компенсации на раздачу — DB-level гарантия,
-- не зависящая от клиентского op_id.
ALTER TABLE blackjack_rounds
    ADD COLUMN IF NOT EXISTS admin_comp_op_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS blackjack_rounds_comp_uniq
    ON blackjack_rounds (admin_comp_op_id)
    WHERE admin_comp_op_id IS NOT NULL;
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
-- Самолечение карточек: подтверждённая версия отстаёт от version,
-- зависший render-op (lease истёк) или подошло время ре-верификации.
CREATE INDEX IF NOT EXISTS blackjack_card_sync_idx
    ON blackjack_rounds (updated_at)
    WHERE message_id IS NOT NULL
      AND status IN ('active','settled')
      AND (card_rendered < version
           OR card_render_op IS NOT NULL
           OR card_verify_after IS NOT NULL);
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


class PayoutNotGuaranteed(Exception):
    """Баланс пользователя не позволяет гарантировать полную выплату
    принятой ставки (потолок SAFE_BALANCE_CAP) — ставка отклонена
    ДО списания."""


class BalanceOverflow(Exception):
    """Гарантированное начисление превысило бы HARD_BALANCE_LIMIT —
    транзакция откатывается целиком (ledger не остаётся)."""


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
    card_render_op: str | None = None      # активная render-операция
    card_verify_after: datetime | None = None  # отложенная ре-верификация
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
        card_render_op=row.get("card_render_op"),
        card_verify_after=row.get("card_verify_after"),
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
        # Гарантия выплаты: после списания на балансе должно хватить
        # места для максимальной выплаты раздачи (2×stake gross).
        # Проверка под FOR UPDATE той же строки — параллельные
        # начисления/переводы сериализованы этой блокировкой, а все
        # не-гарантированные кредиты ограничены SAFE_BALANCE_CAP
        # (user_balance_store). Иначе ставка отклоняется ДО списания.
        from storage.user_balance_store import SAFE_BALANCE_CAP
        if stake > 0 and balance + stake > SAFE_BALANCE_CAP:
            raise PayoutNotGuaranteed(
                f"balance {balance} cannot guarantee payout for stake {stake}"
            )
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
        # user_balance.balance — INTEGER (max 2 147 483 647).
        # Инвариант: balance ≤ SAFE_BALANCE_CAP после любого
        # не-гарантированного кредита (transfer/admin/daily/arcade —
        # все ограничены там), а ставка принимается только если
        # balance + stake ≤ SAFE_BALANCE_CAP (_debit_in_tx).
        # Следовательно гарантированная выплата (≤2×stake ≤ 20 000)
        # всегда помещается в HARD_BALANCE_LIMIT — молчаливого
        # обрезания нет: переполнение → BalanceOverflow → откат tx.
        if cap_exempt:
            from storage.user_balance_store import HARD_BALANCE_LIMIT
            if balance + amount > HARD_BALANCE_LIMIT:
                raise BalanceOverflow(
                    f"guaranteed credit {amount} would exceed "
                    f"{HARD_BALANCE_LIMIT} (balance {balance})"
                )
            add = amount
        else:
            cap = _max_balance()
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
        # Абсолютный технический максимум: гарантия выплаты
        # (balance + stake <= SAFE_BALANCE_CAP → 2×stake <=
        # HARD_BALANCE_LIMIT) доказана только для stake <= BET_MAX.
        # Конфиг economy.blackjack_max_bet не может его превысить.
        if stake > BET_MAX:
            raise ValueError(f"stake exceeds technical max {BET_MAX}")
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

    async def mark_message(
        self, round_id: str, message_id: int,
        sent_version: int | None = None,
    ) -> None:
        """Доставка подтверждена: message_id + delivery_state='delivered'.

        ``sent_version`` — версия состояния, из которой реально
        построено отправленное сообщение (между build и send состояние
        могло измениться). card_rendered фиксирует ИМЕННО её: если БД
        уже новее — list_needing_sync догонит отображение. ``None``
        (reconcile нашёл старое сообщение) → -1: форсируем один
        re-render актуального состояния."""
        rendered = sent_version if sent_version is not None else -1
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE blackjack_rounds
                   SET message_id=$2, delivery_state='delivered',
                       card_rendered=$3, card_claim=$3,
                       card_render_op=NULL, card_render_started_at=NULL,
                       card_verify_after=NULL, updated_at=now()
                   WHERE round_id=$1 AND message_id IS NULL""",
                round_id, message_id, rendered,
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
    # Single-writer lease: у раздачи не более ОДНОЙ активной
    # render-операции (card_render_op + started_at). Новая версия
    # НЕ начинает edit, пока предыдущая операция не завершена
    # (finish) или не истёк lease (крэш → takeover). Discord не
    # проверяет fencing token — поэтому после takeover назначается
    # card_verify_after: отложенный повторный рендер актуального
    # состояния закрывает окно «зомби-HTTP завершился позже».
    # Накопившиеся версии объединяются: render всегда строится из
    # свежего состояния БД, промежуточные версии пропускаются.
    # PG-транзакция не удерживается на время Discord HTTP.

    async def claim_card_render(
        self, round_id: str, op_id: str,
        lease_seconds: int = RENDER_LEASE_SECONDS,
    ) -> bool:
        """Захватить эксклюзивное право рендера карточки.

        Атомарно: нет активной операции ИЛИ её lease истёк, и есть
        что показывать (card_rendered < version либо подошло время
        verify_after). Захват фиксирует card_claim = текущей version —
        рендер всегда строится из свежего состояния (coalescing).
        Takeover истёкшего op назначает card_verify_after — повторная
        проверка актуальности после окна зомби-HTTP.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds SET
                       card_claim = version,
                       card_render_op = $2,
                       card_render_started_at = now(),
                       card_verify_after = CASE
                           WHEN card_render_op IS NOT NULL
                            AND card_render_started_at
                                < now() - make_interval(secs => $3)
                           THEN now() + make_interval(secs => $3 * 2)
                           ELSE card_verify_after END,
                       updated_at = now()
                   WHERE round_id=$1 AND message_id IS NOT NULL
                     AND (card_rendered < version
                          OR (card_verify_after IS NOT NULL
                              AND card_verify_after <= now()))
                     AND (card_render_op IS NULL
                          OR card_render_started_at
                             < now() - make_interval(secs => $3))
                   RETURNING round_id""",
                round_id, op_id, lease_seconds,
            )
            return row is not None

    async def finish_card_render(
        self, round_id: str, op_id: str, ok: bool
    ) -> bool:
        """Завершить render-операцию. Привязано к op_id — устаревший
        worker НЕ может завершить чужую операцию.

        ok=True → card_rendered = card_claim (подтверждённая версия);
        если version успела уйти вперёд — list_needing_sync догонит.
        card_verify_after сбрасывается только если эта операция и
        была запланированной верификацией (она завершилась после
        verify_after) — иначе отложенная проверка остаётся.
        ok=False → операция освобождается без подтверждения:
        следующий sync повторит с актуальным состоянием.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            if ok:
                row = await conn.fetchrow(
                    """UPDATE blackjack_rounds SET
                           card_rendered = card_claim,
                           card_render_op = NULL,
                           card_render_started_at = NULL,
                           card_verify_after = CASE
                               WHEN card_verify_after IS NOT NULL
                                AND card_verify_after <= now()
                               THEN NULL ELSE card_verify_after END,
                           updated_at = now()
                       WHERE round_id=$1 AND card_render_op=$2
                       RETURNING round_id""",
                    round_id, op_id,
                )
            else:
                row = await conn.fetchrow(
                    """UPDATE blackjack_rounds SET
                           card_render_op = NULL,
                           card_render_started_at = NULL,
                           updated_at = now()
                       WHERE round_id=$1 AND card_render_op=$2
                       RETURNING round_id""",
                    round_id, op_id,
                )
            return row is not None

    async def mark_card_closed(self, round_id: str) -> bool:
        """«Закрыть»: кнопки снимаются навсегда, результат остаётся.

        Идемпотентно и только для завершённой раздачи. Bump version —
        закрытое состояние отличается от показанного → ближайший
        sync отрендерит карточку без кнопок. Повторный вызов → False,
        финансовых изменений нет."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds
                   SET card_closed=TRUE, version=version+1,
                       updated_at=now()
                   WHERE round_id=$1 AND status='settled'
                     AND NOT card_closed
                   RETURNING round_id""",
                round_id,
            )
            return row is not None

    async def list_needing_sync(self, limit: int = 25) -> list[StoredRound]:
        """Карточки, требующие синхронизации отображения:

        - card_rendered < version — подтверждённо показана старая
          версия (sync по ПОДТВЕРЖДЁННОЙ версии, не по claim —
          зависший claim не блокирует карточку навсегда);
        - card_render_op с истёкшим lease — зависшая операция
          (крах между claim и finish) → takeover;
        - card_verify_after <= now() — отложенная ре-верификация
          после takeover (зомби-HTTP мог перезаписать карточку).
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM blackjack_rounds
                   WHERE message_id IS NOT NULL
                     AND status IN ('active','settled')
                     AND (card_rendered < version
                          OR (card_render_op IS NOT NULL
                              AND card_render_started_at
                                  < now() - make_interval(
                                      secs => $2))
                          OR (card_verify_after IS NOT NULL
                              AND card_verify_after <= now()))
                   ORDER BY updated_at ASC
                   LIMIT $1""",
                limit, RENDER_LEASE_SECONDS,
            )
            return [_row_to_round(r) for r in rows]

    async def request_card_resync(self, round_id: str) -> bool:
        """Админ: пометить карточку к повторному отображению.

        НЕ трогает активную render-операцию (card_render_op/
        started_at): in-flight Discord HTTP edit завершается штатно
        под своим op_id — параллельный edit не запускается.
        card_rendered=-1 форсирует ближайший sync; дополнительно
        card_verify_after = now()+lease гарантирует, что finish
        активной операции (которая перезаписывает card_rendered
        подтверждённой версией) НЕ поглотит запрос resync —
        отложенная ре-верификация актуального состояния
        отрендерит карточку ещё раз после окна операции.
        Финансовый результат не изменяется; закрытая карточка
        перерисовывается без кнопок (build_game_view + card_closed).
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds
                   SET card_rendered=-1,
                       card_verify_after = now() + make_interval(
                           secs => $2),
                       updated_at=now()
                   WHERE round_id=$1 AND message_id IS NOT NULL
                   RETURNING round_id""",
                round_id, RENDER_LEASE_SECONDS,
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
        проход sweeper снова проверит доставку по маркеру.
        Запрещено для раздачи с уже проведённой компенсацией —
        recheck не должен обходить защиту от повторной выплаты."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE blackjack_rounds
                   SET delivery_state='unknown', reconcile_attempts=0,
                       last_reconcile_at=NULL,
                       dispute_resolved_at=NULL, dispute_resolved_by=NULL,
                       dispute_note=NULL, updated_at=now()
                   WHERE round_id=$1 AND delivery_state='disputed'
                     AND admin_comp_op_id IS NULL
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

        Одна tx под FOR UPDATE проверяет ВСЁ: существование раздачи,
        открытый спор (delivery_state='disputed'), отсутствие
        предыдущего разрешения и предыдущей компенсации
        (admin_comp_op_id IS NULL — DB-уровень, UNIQUE-индекс).
        ledger op='admin_comp:<op_id>' (UNIQUE → повтор с тем же
        op_id невозможен) + cap-exempt credit + отметка решения
        спора с актёром/причиной — атомарно. Карты, исход и
        первоначальная выплата НЕ меняются. Не более одной
        компенсации на раздачу — даже с разными op_id.
        """
        if amount <= 0:
            raise ValueError("amount must be positive")
        from storage.user_balance_store import SAFE_BALANCE_CAP
        if amount > SAFE_BALANCE_CAP:
            raise ValueError("amount exceeds safe balance cap")
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
            # Повтор с тем же op_id — duplicate в любом состоянии
            # спора (до проверок ниже).
            dup = await conn.fetchrow(
                "SELECT id FROM blackjack_ledger "
                "WHERE round_id=$1 AND op=$2", round_id, ledger_op)
            if dup is not None:
                return {"status": "duplicate"}
            if (
                row["delivery_state"] != "disputed"
                or row["dispute_resolved_at"] is not None
                or row["admin_comp_op_id"] is not None
            ):
                return {"status": "not_compensable"}
            # Полнота начисления: баланс + сумма не должны превысить
            # SAFE_BALANCE_CAP — иначе отклоняем ДО ledger/кредита.
            bal_row = await conn.fetchrow(
                "SELECT balance FROM user_balance "
                "WHERE guild_id=$1 AND user_id=$2 FOR UPDATE",
                int(row["guild_id"]), int(row["user_id"]),
            )
            if bal_row is not None and (
                int(bal_row["balance"]) + amount > SAFE_BALANCE_CAP
            ):
                return {"status": "cap_exceeded"}
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
                       dispute_note=$3, admin_comp_op_id=$4,
                       updated_at=now()
                   WHERE round_id=$1""",
                round_id, actor_id,
                f"compensation +{amount}: {reason}"[:500], op_id,
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
