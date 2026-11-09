"""Хранилище Reputation System 2.0 — PostgreSQL.

Таблицы (создаются в storage/db.py::init_db):
- reputation_transactions: журнал всех начислений REP,
  event_key UNIQUE — защита от повторных операций.
- rep_giver_state: скользящий cooldown /rep (24ч) на отправителя.
- rep_invites: кандидаты и подтверждённые награды за приглашения,
  UNIQUE(guild_id, invitee_id) — один приглашённый = одна награда.
- rep_boosts: периоды проверки буста, UNIQUE(guild_id, booster_id).

Начисление REP и запись журнала выполняются в одной транзакции
PostgreSQL — отдельного JSON-fallback нет (как player_stats_store).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from storage.db import get_pool

logger = logging.getLogger(__name__)

# Источники операций
SRC_MANUAL = "MANUAL_REP"
SRC_INVITE = "INVITE"
SRC_BOOST = "BOOST"
SRC_ADMIN = "ADMIN_ADJUSTMENT"
SRC_SERVER_TAG = "SERVER_TAG"


@dataclass
class ManualRepResult:
    """Результат попытки выдачи /rep."""

    status: str  # ok | already_pair | cooldown | error
    rep_after: int = 0
    next_available_at: datetime | None = None
    cooldown_seconds: int = 0
    transaction_id: int = 0  # id записи reputation_transactions (для outbox)


class ReputationStore:
    """Все операции репутации — через пул asyncpg."""

    # ------------------------------------------------------------------
    # /rep — атомарная выдача
    # ------------------------------------------------------------------

    async def try_give_manual_rep(
        self,
        guild_id: int,
        giver_id: int,
        target_id: int,
        amount: int,
        cooldown: timedelta,
        target_name: str = "",
    ) -> ManualRepResult:
        """Атомарно: проверить cooldown/пару → журнал → +REP → cooldown.

        Блокировки: rep_giver_state строка FOR UPDATE + UNIQUE event_key.
        """
        event_key = f"manual_rep:{guild_id}:{giver_id}:{target_id}"
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Строка состояния выдающего (создаём при первом /rep)
                await conn.execute(
                    """
                    INSERT INTO rep_giver_state (guild_id, giver_id, last_rep_at)
                    VALUES ($1, $2, NULL)
                    ON CONFLICT (guild_id, giver_id) DO NOTHING
                    """,
                    guild_id, giver_id,
                )
                row = await conn.fetchrow(
                    """
                    SELECT last_rep_at FROM rep_giver_state
                    WHERE guild_id = $1 AND giver_id = $2
                    FOR UPDATE
                    """,
                    guild_id, giver_id,
                )
                last = row["last_rep_at"] if row else None
                now = datetime.now(timezone.utc)
                if last is not None:
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    available_at = last + cooldown
                    if now < available_at:
                        return ManualRepResult(
                            status="cooldown",
                            next_available_at=available_at,
                            cooldown_seconds=max(0, int((available_at - now).total_seconds())),
                        )

                # Журнал: UNIQUE event_key — повтор той же пары невозможен
                tx_id = await conn.fetchval(
                    """
                    INSERT INTO reputation_transactions
                        (guild_id, user_id, source, amount,
                         actor_user_id, subject_user_id, event_key)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (event_key) DO NOTHING
                    RETURNING id
                    """,
                    guild_id, target_id, SRC_MANUAL, amount,
                    giver_id, target_id, event_key,
                )
                if tx_id is None:
                    return ManualRepResult(status="already_pair")

                # +REP получателю (upsert player_stats)
                rep_after = await conn.fetchval(
                    """
                    INSERT INTO player_stats (guild_id, user_id, name, reputation)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET reputation = player_stats.reputation + $4
                    RETURNING reputation
                    """,
                    guild_id, target_id, target_name, amount,
                )
                rep_after = int(rep_after or 0)

                await conn.execute(
                    """
                    UPDATE rep_giver_state SET last_rep_at = now()
                    WHERE guild_id = $1 AND giver_id = $2
                    """,
                    guild_id, giver_id,
                )
                await _enqueue_rep_log(
                    conn,
                    transaction_id=int(tx_id),
                    guild_id=guild_id,
                    user_id=target_id,
                    source=SRC_MANUAL,
                    amount=amount,
                    balance_after=rep_after,
                    actor_user_id=giver_id,
                    subject_user_id=target_id,
                )
                return ManualRepResult(
                    status="ok", rep_after=rep_after, transaction_id=int(tx_id)
                )

    # ------------------------------------------------------------------
    # Начисление REP (invite / boost / admin) — одна транзакция
    # ------------------------------------------------------------------

    async def award_rep(
        self,
        guild_id: int,
        user_id: int,
        source: str,
        amount: int,
        event_key: str,
        actor_user_id: int = 0,
        subject_user_id: int = 0,
        user_name: str = "",
    ) -> bool:
        """Начислить REP с записью в журнал. False — event_key уже был."""
        result = await self.award_rep_logged(
            guild_id=guild_id,
            user_id=user_id,
            source=source,
            amount=amount,
            event_key=event_key,
            actor_user_id=actor_user_id,
            subject_user_id=subject_user_id,
            user_name=user_name,
        )
        return result is not None

    async def award_rep_logged(
        self,
        guild_id: int,
        user_id: int,
        source: str,
        amount: int,
        event_key: str,
        actor_user_id: int = 0,
        subject_user_id: int = 0,
        user_name: str = "",
    ) -> dict[str, Any] | None:
        """Начислить REP + поставить outbox-лог в той же транзакции.

        Возвращает {transaction_id, rep_after} или None при дубликате.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                tx_id = await conn.fetchval(
                    """
                    INSERT INTO reputation_transactions
                        (guild_id, user_id, source, amount,
                         actor_user_id, subject_user_id, event_key)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (event_key) DO NOTHING
                    RETURNING id
                    """,
                    guild_id, user_id, source, amount,
                    actor_user_id, subject_user_id, event_key,
                )
                if tx_id is None:
                    return None
                rep_after = await conn.fetchval(
                    """
                    INSERT INTO player_stats (guild_id, user_id, name, reputation)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET reputation = player_stats.reputation + $4
                    RETURNING reputation
                    """,
                    guild_id, user_id, user_name, amount,
                )
                rep_after = int(rep_after or 0)
                await _enqueue_rep_log(
                    conn,
                    transaction_id=int(tx_id),
                    guild_id=guild_id,
                    user_id=user_id,
                    source=source,
                    amount=amount,
                    balance_after=rep_after,
                    actor_user_id=actor_user_id,
                    subject_user_id=subject_user_id,
                )
                return {"transaction_id": int(tx_id), "rep_after": rep_after}

    # ------------------------------------------------------------------
    # Журнал и аналитика
    # ------------------------------------------------------------------

    async def get_transactions(
        self, guild_id: int, user_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT source, amount, actor_user_id, created_at
                FROM reputation_transactions
                WHERE guild_id = $1 AND user_id = $2
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                guild_id, user_id, limit,
            )
        return [dict(r) for r in rows]

    async def get_source_breakdown(
        self, guild_id: int, user_id: int
    ) -> dict[str, int]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT source, COALESCE(SUM(amount), 0) AS total
                FROM reputation_transactions
                WHERE guild_id = $1 AND user_id = $2
                GROUP BY source
                """,
                guild_id, user_id,
            )
        return {r["source"]: int(r["total"]) for r in rows}

    async def count_manual_given(self, guild_id: int, giver_id: int) -> int:
        """Сколько разных получателей получил giver через /rep."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(
                """
                SELECT COUNT(DISTINCT user_id) FROM reputation_transactions
                WHERE guild_id = $1 AND actor_user_id = $2 AND source = $3
                """,
                guild_id, giver_id, SRC_MANUAL,
            ) or 0)

    async def get_rep_leaderboard(
        self, guild_id: int, page: int = 1, per_page: int = 10
    ) -> list[dict[str, Any]]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, name, reputation
                FROM player_stats
                WHERE guild_id = $1 AND reputation > 0 AND user_id > 0
                ORDER BY reputation DESC, user_id ASC
                LIMIT $2 OFFSET $3
                """,
                guild_id, per_page, (page - 1) * per_page,
            )
        return [dict(r) for r in rows]

    async def get_rep_leaderboard_count(self, guild_id: int) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(
                "SELECT COUNT(*) FROM player_stats"
                " WHERE guild_id = $1 AND reputation > 0 AND user_id > 0",
                guild_id,
            ) or 0)

    async def get_player_rep_rank(self, guild_id: int, user_id: int) -> int | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT reputation FROM player_stats WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id,
            )
            if row is None or int(row["reputation"] or 0) <= 0:
                return None
            rep = int(row["reputation"])
            rank = await conn.fetchval(
                """
                SELECT COUNT(*) + 1 FROM player_stats
                WHERE guild_id = $1 AND user_id > 0 AND (
                    reputation > $2
                    OR (reputation = $2 AND user_id < $3)
                )
                """,
                guild_id, rep, user_id,
            )
            return int(rank)

    async def get_guild_rep_stats(self, guild_id: int) -> dict[str, Any]:
        """Общая статистика репутации сервера."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            totals = await conn.fetch(
                """
                SELECT source, COUNT(*) AS ops, COALESCE(SUM(amount), 0) AS total
                FROM reputation_transactions
                WHERE guild_id = $1
                GROUP BY source
                """,
                guild_id,
            )
            users_with_rep = await conn.fetchval(
                "SELECT COUNT(*) FROM player_stats"
                " WHERE guild_id = $1 AND reputation > 0",
                guild_id,
            )
            legacy = await conn.fetchval(
                """
                SELECT COALESCE(SUM(GREATEST(reputation - COALESCE(tx.total, 0), 0)), 0)
                FROM player_stats ps
                LEFT JOIN (
                    SELECT user_id, SUM(amount) AS total
                    FROM reputation_transactions
                    WHERE guild_id = $1
                    GROUP BY user_id
                ) tx ON tx.user_id = ps.user_id
                WHERE ps.guild_id = $1 AND ps.reputation > 0
                """,
                guild_id,
            )
        return {
            "by_source": {r["source"]: {"ops": int(r["ops"]), "total": int(r["total"])} for r in totals},
            "users_with_rep": int(users_with_rep or 0),
            "legacy_rep": int(legacy or 0),
        }

    # ------------------------------------------------------------------
    # Приглашения
    # ------------------------------------------------------------------

    async def _log_invite_event(
        self, conn, invite_id: int, guild_id: int, invitee_id: int,
        old_attribution: str, new_attribution: str,
        old_status: str, new_status: str, inviter_id: int | None,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO rep_invite_events
                (invite_id, guild_id, invitee_id, old_attribution,
                 new_attribution, old_status, new_status, inviter_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            invite_id, guild_id, invitee_id, old_attribution,
            new_attribution, old_status, new_status, inviter_id,
        )

    async def create_invite_candidate(
        self,
        guild_id: int,
        inviter_id: int | None,
        invitee_id: int,
        invite_code: str,
        verify_until: datetime,
        attribution: str,
        reject_reason: str = "",
    ) -> int | None:
        """Создать кандидата на награду с безопасной повторной атрибуцией.

        - Новый invitee → INSERT (pending).
        - Существующая запись pending/rewarded → без изменений (None).
        - rejected/cancelled + новая ДОСТОВЕРНАЯ атрибуция (ok) →
          безопасная переатрибуция: новый inviter/code, новый 72ч срок,
          статус снова pending. Подтверждённого пригласившего
          (rewarded/pending) перезаписать нельзя.
        - rejected/cancelled + неопределённая атрибуция → без изменений.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    """
                    SELECT id, attribution, reward_status, inviter_id
                    FROM rep_invites
                    WHERE guild_id = $1 AND invitee_id = $2
                    FOR UPDATE
                    """,
                    guild_id, invitee_id,
                )
                if existing is None:
                    new_id = await conn.fetchval(
                        """
                        INSERT INTO rep_invites
                            (guild_id, inviter_id, invitee_id, invite_code,
                             verify_until, attribution, reject_reason)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        RETURNING id
                        """,
                        guild_id, inviter_id, invitee_id, invite_code,
                        verify_until, attribution, reject_reason,
                    )
                    await self._log_invite_event(
                        conn, int(new_id), guild_id, invitee_id,
                        "", attribution, "", "pending", inviter_id,
                    )
                    return int(new_id)

                old_status = existing["reward_status"]
                old_attr = existing["attribution"]
                # Переатрибуция только из терминального статуса и только
                # при достоверном 'ok' — rewarded/pending не трогаем.
                if old_status not in ("rejected", "cancelled"):
                    return None
                if attribution != "ok" or inviter_id is None:
                    return None
                await conn.execute(
                    """
                    UPDATE rep_invites SET
                        inviter_id = $3, invite_code = $4,
                        verify_until = $5, attribution = $6,
                        reward_status = 'pending', rewarded_at = NULL,
                        reject_reason = ''
                    WHERE id = $1 AND guild_id = $2
                    """,
                    existing["id"], guild_id, inviter_id, invite_code,
                    verify_until, attribution,
                )
                await self._log_invite_event(
                    conn, int(existing["id"]), guild_id, invitee_id,
                    old_attr, attribution, old_status, "pending", inviter_id,
                )
                return int(existing["id"])

    async def get_invite(self, invite_id: int) -> dict[str, Any] | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM rep_invites WHERE id = $1", invite_id
            )
        return dict(row) if row else None

    async def get_due_invites(self, now: datetime) -> list[dict[str, Any]]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM rep_invites
                WHERE reward_status = 'pending' AND verify_until <= $1
                ORDER BY verify_until ASC
                LIMIT 100
                """,
                now,
            )
        return [dict(r) for r in rows]

    async def update_invite(
        self,
        invite_id: int,
        *,
        attribution: str | None = None,
        reward_status: str | None = None,
        reject_reason: str | None = None,
    ) -> None:
        sets, args = [], []
        if attribution is not None:
            args.append(attribution)
            sets.append(f"attribution = ${len(args)}")
        if reward_status is not None:
            args.append(reward_status)
            sets.append(f"reward_status = ${len(args)}")
            if reward_status == "rewarded":
                sets.append("rewarded_at = now()")
        if reject_reason is not None:
            args.append(reject_reason)
            sets.append(f"reject_reason = ${len(args)}")
        if not sets:
            return
        args.append(invite_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchrow(
                    "SELECT guild_id, invitee_id, attribution,"
                    " reward_status FROM rep_invites WHERE id = $1"
                    " FOR UPDATE",
                    invite_id,
                )
                await conn.execute(
                    f"UPDATE rep_invites SET {', '.join(sets)} WHERE id = ${len(args)}",
                    *args,
                )
                if old is not None:
                    new_status = reward_status or old["reward_status"]
                    new_attr = attribution or old["attribution"]
                    await self._log_invite_event(
                        conn, invite_id, int(old["guild_id"]),
                        int(old["invitee_id"]), old["attribution"],
                        new_attr, old["reward_status"], new_status, None,
                    )

    async def get_user_invite_stats(self, guild_id: int, user_id: int) -> dict[str, Any]:
        """Статистика приглашений пользователя (как пригласившего)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT attribution, reward_status, COUNT(*) AS cnt
                FROM rep_invites
                WHERE guild_id = $1 AND inviter_id = $2
                GROUP BY attribution, reward_status
                """,
                guild_id, user_id,
            )
        stats: dict[str, Any] = {
            "invited": 0, "rewarded": 0, "pending": 0,
            "rejected": 0, "cancelled": 0,
        }
        for r in rows:
            cnt = int(r["cnt"])
            stats["invited"] += cnt
            status = r["reward_status"]
            if status == "rewarded":
                stats["rewarded"] += cnt
            elif status == "pending":
                stats["pending"] += cnt
            elif status == "cancelled":
                stats["cancelled"] += cnt
            else:
                stats["rejected"] += cnt
        return stats

    # ------------------------------------------------------------------
    # Персональные пригласительные ссылки
    # ------------------------------------------------------------------

    async def set_invite_owner(
        self, guild_id: int, code: str, owner_id: int, channel_id: int = 0
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO rep_invite_owners (guild_id, code, owner_id, channel_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, code) DO UPDATE SET
                    owner_id = EXCLUDED.owner_id,
                    channel_id = EXCLUDED.channel_id
                """,
                guild_id, code, owner_id, channel_id,
            )

    # ------------------------------------------------------------------
    # Персональные пригласительные ссылки (одна на пользователя)
    # ------------------------------------------------------------------

    async def get_user_invite(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM rep_user_invites"
                " WHERE guild_id = $1 AND user_id = $2",
                guild_id, user_id,
            )
        return dict(row) if row else None

    async def claim_user_invite(
        self, guild_id: int, user_id: int, channel_id: int
    ) -> bool:
        """Зарезервировать слот персональной ссылки.

        True — слот наш (invite_code ещё пустой, надо создать ссылку).
        False — слот уже занят (ссылка есть или создаётся другим
        процессом) — читать через get_user_invite.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchval(
                """
                INSERT INTO rep_user_invites (guild_id, user_id, channel_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, user_id) DO NOTHING
                RETURNING user_id
                """,
                guild_id, user_id, channel_id,
            )
        return row is not None

    async def set_user_invite(
        self, guild_id: int, user_id: int, code: str, channel_id: int
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO rep_user_invites
                    (guild_id, user_id, invite_code, channel_id, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    invite_code = EXCLUDED.invite_code,
                    channel_id = EXCLUDED.channel_id,
                    updated_at = now()
                """,
                guild_id, user_id, code, channel_id,
            )

    async def replace_user_invite_code(
        self, guild_id: int, user_id: int,
        expected_code: str, new_code: str, channel_id: int,
    ) -> bool:
        """Заменить код только если он всё ещё expected_code.

        Защита от гонки двух процессов, одновременно заменяющих
        удалённую ссылку: UPDATE сработает у одного.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE rep_user_invites SET
                    invite_code = $4, channel_id = $5, updated_at = now()
                WHERE guild_id = $1 AND user_id = $2
                  AND invite_code = $3
                """,
                guild_id, user_id, expected_code, new_code, channel_id,
            )
        # asyncpg возвращает "UPDATE n"
        return res.endswith(" 1") or res == "UPDATE 1"

    async def get_invite_owner(self, guild_id: int, code: str) -> int | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT owner_id FROM rep_invite_owners"
                " WHERE guild_id = $1 AND code = $2",
                guild_id, code,
            )

    async def get_invite_owners(
        self, guild_id: int, codes: list[str]
    ) -> dict[str, int]:
        """Владельцы персональных ссылок для набора кодов."""
        if not codes:
            return {}
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT code, owner_id FROM rep_invite_owners"
                " WHERE guild_id = $1 AND code = ANY($2::text[])",
                guild_id, codes,
            )
        return {r["code"]: int(r["owner_id"]) for r in rows}

    async def get_invite_debug(
        self, guild_id: int, limit: int = 20
    ) -> dict[str, Any]:
        """Админ-диагностика: счётчики по attribution/status + последние записи."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            counts = await conn.fetch(
                """
                SELECT attribution, reward_status, COUNT(*) AS cnt
                FROM rep_invites
                WHERE guild_id = $1
                GROUP BY attribution, reward_status
                ORDER BY attribution, reward_status
                """,
                guild_id,
            )
            recent = await conn.fetch(
                """
                SELECT invitee_id, inviter_id, invite_code, attribution,
                       reward_status, reject_reason, joined_at, verify_until
                FROM rep_invites
                WHERE guild_id = $1
                ORDER BY joined_at DESC
                LIMIT $2
                """,
                guild_id, limit,
            )
        return {
            "counts": [
                {
                    "attribution": r["attribution"],
                    "reward_status": r["reward_status"],
                    "cnt": int(r["cnt"]),
                }
                for r in counts
            ],
            "recent": [dict(r) for r in recent],
        }

    # ------------------------------------------------------------------
    # Бусты
    # ------------------------------------------------------------------

    async def upsert_boost_pending(
        self,
        guild_id: int,
        booster_id: int,
        boost_started_at: datetime,
        verify_until: datetime,
    ) -> bool:
        """Создать/сбросить ожидающий период буста.

        True — период создан/обновлён; False — награда уже получена.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO rep_boosts
                    (guild_id, booster_id, boost_started_at, verify_until, status)
                VALUES ($1, $2, $3, $4, 'pending')
                ON CONFLICT (guild_id, booster_id) DO UPDATE SET
                    boost_started_at = EXCLUDED.boost_started_at,
                    verify_until = EXCLUDED.verify_until,
                    status = 'pending',
                    rewarded_at = NULL
                WHERE rep_boosts.status <> 'rewarded'
                RETURNING id
                """,
                guild_id, booster_id, boost_started_at, verify_until,
            )
            return row is not None

    async def cancel_boost_pending(self, guild_id: int, booster_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE rep_boosts SET status = 'cancelled'
                WHERE guild_id = $1 AND booster_id = $2 AND status = 'pending'
                """,
                guild_id, booster_id,
            )

    async def get_boost(self, guild_id: int, booster_id: int) -> dict[str, Any] | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM rep_boosts WHERE guild_id = $1 AND booster_id = $2",
                guild_id, booster_id,
            )
        return dict(row) if row else None

    async def get_due_boosts(self, now: datetime) -> list[dict[str, Any]]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM rep_boosts
                WHERE status = 'pending' AND verify_until <= $1
                ORDER BY verify_until ASC
                LIMIT 100
                """,
                now,
            )
        return [dict(r) for r in rows]

    async def mark_boost_rewarded(self, guild_id: int, booster_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE rep_boosts SET status = 'rewarded', rewarded_at = now()
                WHERE guild_id = $1 AND booster_id = $2 AND status = 'pending'
                """,
                guild_id, booster_id,
            )

    async def get_boost_status(self, guild_id: int, booster_id: int) -> str | None:
        row = await self.get_boost(guild_id, booster_id)
        return row["status"] if row else None

    # ------------------------------------------------------------------
    # Административная корректировка REP (публично логируется)
    # ------------------------------------------------------------------

    async def admin_adjust_rep(
        self,
        guild_id: int,
        admin_id: int,
        target_id: int,
        amount: int,
        target_name: str = "",
        reason: str = "",
    ) -> dict[str, Any] | None:
        """Изменить REP администратора: журнал + баланс + outbox в одной tx.

        amount может быть отрицательным; баланс не уходит ниже 0.
        event_key генерируется уникальным на каждый вызов.
        """
        import uuid

        event_key = f"admin_adjust:{guild_id}:{uuid.uuid4()}"
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                tx_id = await conn.fetchval(
                    """
                    INSERT INTO reputation_transactions
                        (guild_id, user_id, source, amount,
                         actor_user_id, subject_user_id, event_key)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING id
                    """,
                    guild_id, target_id, SRC_ADMIN, amount,
                    admin_id, target_id, event_key,
                )
                rep_after = await conn.fetchval(
                    """
                    INSERT INTO player_stats (guild_id, user_id, name, reputation)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET reputation = GREATEST(
                        player_stats.reputation + $4, 0)
                    RETURNING reputation
                    """,
                    guild_id, target_id, target_name, amount,
                )
                rep_after = int(rep_after or 0)
                await _enqueue_rep_log(
                    conn,
                    transaction_id=int(tx_id),
                    guild_id=guild_id,
                    user_id=target_id,
                    source=SRC_ADMIN,
                    amount=amount,
                    balance_after=rep_after,
                    actor_user_id=admin_id,
                    subject_user_id=target_id,
                )
                return {"transaction_id": int(tx_id), "rep_after": rep_after}

    # ------------------------------------------------------------------
    # Outbox публичных логов
    # ------------------------------------------------------------------

    async def get_pending_rep_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM rep_log_outbox
                WHERE log_status = 'pending'
                ORDER BY id ASC
                LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]

    async def mark_rep_log_sent(
        self, outbox_id: int, message_id: int
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE rep_log_outbox
                SET log_status = 'sent', log_message_id = $2,
                    published_at = now(), attempts = attempts + 1,
                    last_error = ''
                WHERE id = $1
                """,
                outbox_id, message_id,
            )

    async def mark_rep_log_failed(self, outbox_id: int, error: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE rep_log_outbox
                SET attempts = attempts + 1, last_error = $2
                WHERE id = $1 AND log_status = 'pending'
                """,
                outbox_id, (error or "")[:500],
            )

    async def count_pending_rep_logs(self) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(
                "SELECT COUNT(*) FROM rep_log_outbox WHERE log_status = 'pending'"
            ) or 0)

    # ------------------------------------------------------------------
    # Настройки каналов (bot_settings)
    # ------------------------------------------------------------------

    async def get_setting(self, guild_id: int, key: str) -> str | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT value FROM bot_settings WHERE guild_id = $1 AND key = $2",
                guild_id, key,
            )

    async def set_setting(self, guild_id: int, key: str, value: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO bot_settings (guild_id, key, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id, key) DO UPDATE SET value = $3
                """,
                guild_id, key, value,
            )


async def _enqueue_rep_log(
    conn: Any,
    *,
    transaction_id: int,
    guild_id: int,
    user_id: int,
    source: str,
    amount: int,
    balance_after: int,
    actor_user_id: int,
    subject_user_id: int,
) -> None:
    """Поставить событие публичного лога в outbox (внутри tx REP).

    Вызывается только для реально созданных транзакций — дубликаты
    event_key отсекаются раньше, поэтому повторной постановки нет.
    """
    await conn.execute(
        """
        INSERT INTO rep_log_outbox
            (transaction_id, guild_id, user_id, source, amount,
             balance_after, actor_user_id, subject_user_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (transaction_id) DO NOTHING
        """,
        transaction_id, guild_id, user_id, source, amount,
        balance_after, actor_user_id, subject_user_id,
    )


reputation_store = ReputationStore()
