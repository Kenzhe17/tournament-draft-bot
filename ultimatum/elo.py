"""ELO «Ультиматума» 2.0 — независимые рейтинги Соло и Дуо.

Классический ELO: E = 1/(1+10^((Rb-Ra)/400)), Δ = K*(S-E), K=32,
старт 1000, минимум 100.

Соло: прямое сравнение рейтингов двух игроков.
Дуо: рейтинг команды в матче = средний ELO её игроков; изменение
применяется к каждому игроку индивидуально относительно среднего
ELO команды соперника.

Идемпотентность: UNIQUE(match_id, player_id) в ult_elo_history +
проверка существования записи перед начислением.
"""

from __future__ import annotations

import logging
from typing import Optional

import asyncpg

from storage.db import get_pool

logger = logging.getLogger(__name__)

STARTING_ELO = 1000
MIN_ELO = 100
K_FACTOR = 32


def expected_score(rating_a: int, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def _delta(rating: int, opp_avg: float, score: float) -> int:
    exp = expected_score(rating, opp_avg)
    return round(K_FACTOR * (score - exp))


class UltEloStore:
    """Рейтинги Ультиматума по форматам (solo/duo)."""

    # ---------- чтение ----------

    async def get_rating(
        self, discord_id: int, fmt: str
    ) -> Optional[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM ult_elo_ratings WHERE discord_id=$1 AND format=$2",
                discord_id, fmt,
            )
        return dict(r) if r else None

    async def get_or_create(
        self, discord_id: int, fmt: str, conn: Optional[asyncpg.Connection] = None
    ) -> dict:
        async def _get(c):
            r = await c.fetchrow(
                """
                INSERT INTO ult_elo_ratings (discord_id, format, elo, peak_elo)
                VALUES ($1,$2,$3,$3)
                ON CONFLICT (discord_id, format) DO NOTHING
                RETURNING *
                """,
                discord_id, fmt, STARTING_ELO,
            )
            if r is None:
                r = await c.fetchrow(
                    "SELECT * FROM ult_elo_ratings WHERE discord_id=$1 AND format=$2",
                    discord_id, fmt,
                )
            return dict(r)
        if conn is not None:
            return await _get(conn)
        pool = await get_pool()
        async with pool.acquire() as c:
            return await _get(c)

    async def get_history(
        self, discord_id: int, fmt: str, limit: int = 10
    ) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ult_elo_history
                WHERE player_id=$1 AND format=$2
                ORDER BY id DESC LIMIT $3
                """,
                discord_id, fmt, limit,
            )
        return [dict(r) for r in rows]

    async def get_leaderboard(
        self, fmt: str, limit: int = 10, offset: int = 0
    ) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ult_elo_ratings
                WHERE format=$1 ORDER BY elo DESC LIMIT $2 OFFSET $3
                """,
                fmt, limit, offset,
            )
        return [dict(r) for r in rows]

    async def get_leaderboard_count(self, fmt: str = "solo") -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM ult_elo_ratings WHERE format=$1", fmt
            )

    # ---------- миграция легаси ----------

    async def migrate_legacy_solo(self) -> int:
        """Разовый перенос старого Соло ELO (elo_ratings) в
        ult_elo_ratings(format='solo'). История elo_history остаётся
        в старой таблице для чтения — не переносится и не меняется.
        Возвращает число перенесённых строк."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                INSERT INTO ult_elo_ratings
                    (discord_id, format, elo, peak_elo, rated_matches,
                     wins, losses, created_at, updated_at)
                SELECT discord_id, 'solo', elo, peak_elo, rated_matches,
                       wins, losses, created_at, updated_at
                FROM elo_ratings
                ON CONFLICT (discord_id, format) DO NOTHING
                """
            )
        try:
            return int(res.split()[-1])
        except Exception:
            return 0

    # ---------- начисление ----------

    async def apply_match(
        self,
        conn: asyncpg.Connection,
        *,
        match_db_id: int,
        tournament_id: str,
        fmt: str,
        winner_member_ids: list[int],
        loser_member_ids: list[int],
    ) -> int:
        """Начислить ELO за завершённый матч. Вызывать внутри
        транзакции завершения матча. Возвращает число обновлённых
        рейтингов (0 — уже начислено, защита от дабл-списания)."""
        # Проверка идемпотентности
        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM ult_elo_history WHERE match_id=$1",
            match_db_id,
        )
        total_expected = len(winner_member_ids) + len(loser_member_ids)
        if existing >= total_expected:
            return 0

        async def rating(uid: int) -> dict:
            return await self.get_or_create(uid, fmt, conn=conn)

        winners = [await rating(u) for u in winner_member_ids]
        losers = [await rating(u) for u in loser_member_ids]
        win_avg = sum(w["elo"] for w in winners) / len(winners)
        lose_avg = sum(l["elo"] for l in losers) / len(losers)

        applied = 0
        for w in winners:
            uid = w["discord_id"]
            if await self._already(conn, match_db_id, uid):
                continue
            change = _delta(w["elo"], lose_avg, 1.0)
            new_elo = max(MIN_ELO, w["elo"] + change)
            change = new_elo - w["elo"]
            await self._write(conn, uid, fmt, new_elo, w, "WIN",
                              match_db_id, tournament_id, win_avg, lose_avg,
                              change)
            applied += 1
        for l in losers:
            uid = l["discord_id"]
            if await self._already(conn, match_db_id, uid):
                continue
            change = _delta(l["elo"], win_avg, 0.0)
            new_elo = max(MIN_ELO, l["elo"] + change)
            change = new_elo - l["elo"]
            await self._write(conn, uid, fmt, new_elo, l, "LOSS",
                              match_db_id, tournament_id, lose_avg, win_avg,
                              change)
            applied += 1
        return applied

    @staticmethod
    async def _already(conn, match_db_id: int, uid: int) -> bool:
        return bool(await conn.fetchval(
            "SELECT 1 FROM ult_elo_history WHERE match_id=$1 AND player_id=$2",
            match_db_id, uid,
        ))

    @staticmethod
    async def _write(
        conn, uid: int, fmt: str, new_elo: int, old: dict,
        result: str, match_db_id: int, tournament_id: str,
        side_avg: float, opp_avg: float, change: int,
    ) -> None:
        await conn.execute(
            """
            UPDATE ult_elo_ratings SET
                elo=$3,
                peak_elo=GREATEST(peak_elo,$3),
                rated_matches=rated_matches+1,
                wins=wins + $4,
                losses=losses + $5,
                updated_at=now()
            WHERE discord_id=$1 AND format=$2
            """,
            uid, fmt, new_elo,
            1 if result == "WIN" else 0,
            1 if result == "LOSS" else 0,
        )
        await conn.execute(
            """
            INSERT INTO ult_elo_history
                (match_id, tournament_id, format, player_id, result,
                 side_avg_elo, opp_avg_elo, elo_before, elo_after, elo_change)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (match_id, player_id) DO NOTHING
            """,
            match_db_id, tournament_id, fmt, uid, result,
            round(side_avg, 2), round(opp_avg, 2),
            old["elo"], new_elo, change,
        )


ult_elo_store = UltEloStore()
