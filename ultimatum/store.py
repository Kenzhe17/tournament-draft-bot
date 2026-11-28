"""PostgreSQL store для «Ультиматум» 2.0 (ult2_* таблицы).

Источник истины — PostgreSQL. In-memory Bracket используется
для шаблонов/симуляций; живой турнир полностью восстанавливается
из ult2_* таблиц после перезапуска.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from storage.db import get_pool
from ultimatum.bracket import Format, MatchStatus, Slot, TEMPLATES

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("REGISTRATION", "IN_PROGRESS")
FINISHED_STATUSES = ("COMPLETED", "CANCELLED")


def _uid() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class UltTournament:
    id: str
    format: Format
    guild_id: int
    name: str
    status: str = "REGISTRATION"
    is_open: bool = False
    channel_id: int = 0
    message_id: int = 0
    bracket_message_id: int = 0
    created_by: int = 0
    is_test: bool = False
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    champion_entrant_id: Optional[int] = None


@dataclass
class UltEntrant:
    id: int
    tournament_id: str
    seed: Optional[int]
    member_ids: list[int]
    member_names: list[str]
    wins: int = 0
    losses: int = 0
    status: str = "ACTIVE"
    final_place: Optional[int] = None
    place_group: Optional[str] = None

    def label(self) -> str:
        if len(self.member_ids) == 1:
            return self.member_names[0] if self.member_names else f"<{self.member_ids[0]}>"
        if self.member_names:
            return " & ".join(self.member_names)
        return " & ".join(f"<{m}>" for m in self.member_ids)


@dataclass
class UltMatch:
    id: int
    tournament_id: str
    key: str
    bracket: str
    round_name: str
    gf_part: int
    status: str
    side_a_src: str
    side_b_src: str
    entrant_a: Optional[int] = None
    entrant_b: Optional[int] = None
    winner_entrant: Optional[int] = None
    loser_entrant: Optional[int] = None
    room_id: Optional[str] = None
    room_password: Optional[str] = None
    room_updated_by: Optional[int] = None
    room_updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class UltVote:
    id: int
    match_id: int
    user_id: int
    voter_entrant: Optional[int]
    voted_entrant: int
    created_at: Optional[datetime] = None


@dataclass
class DuoInvite:
    id: int
    tournament_id: str
    captain_id: int
    partner_id: Optional[int]
    partner_name: str
    status: str
    message_id: int
    expires_at: datetime
    created_at: Optional[datetime] = None


def _tournament_from_row(r) -> UltTournament:
    return UltTournament(
        id=r["id"], format=Format(r["format"]), guild_id=r["guild_id"],
        name=r["name"], status=r["status"], is_open=r["is_open"],
        channel_id=r["channel_id"], message_id=r["message_id"],
        bracket_message_id=r["bracket_message_id"], created_by=r["created_by"],
        created_at=r["created_at"], started_at=r["started_at"],
        completed_at=r["completed_at"],
        champion_entrant_id=r["champion_entrant_id"],
        is_test=bool(r.get("is_test", False)),
    )


def _decode_names(value) -> list[str]:
    """member_names хранится как JSONB — asyncpg отдаёт str."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    return list(value or [])


def _entrant_from_row(r) -> UltEntrant:
    return UltEntrant(
        id=r["id"], tournament_id=r["tournament_id"], seed=r["seed"],
        member_ids=[int(x) for x in (r["member_ids"] or [])],
        member_names=_decode_names(r["member_names"]),
        wins=r["wins"], losses=r["losses"], status=r["status"],
        final_place=r["final_place"], place_group=r["place_group"],
    )


def _match_from_row(r) -> UltMatch:
    return UltMatch(
        id=r["id"], tournament_id=r["tournament_id"], key=r["key"],
        bracket=r["bracket"], round_name=r["round_name"],
        gf_part=r["gf_part"], status=r["status"],
        side_a_src=r["side_a_src"], side_b_src=r["side_b_src"],
        entrant_a=r["entrant_a"], entrant_b=r["entrant_b"],
        winner_entrant=r["winner_entrant"], loser_entrant=r["loser_entrant"],
        room_id=r["room_id"], room_password=r["room_password"],
        room_updated_by=r["room_updated_by"], room_updated_at=r["room_updated_at"],
        created_at=r["created_at"], completed_at=r["completed_at"],
    )


def slot_to_str(slot: Slot) -> str:
    return f"{slot.kind}:{slot.ref}"


def str_to_slot(src: str) -> Slot:
    kind, _, ref = src.partition(":")
    return Slot(kind, ref)


class UltimatumStore:
    """CRUD для ult2_* таблиц."""

    # ---------------- ТУРНИРЫ ----------------

    async def create_tournament(
        self, fmt: Format, guild_id: int, name: str, created_by: int,
        channel_id: int = 0, is_open: bool = False, is_test: bool = False,
    ) -> UltTournament:
        tid = _uid()
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO ult2_tournaments
                    (id, format, guild_id, name, status, is_open,
                     channel_id, created_by, is_test)
                VALUES ($1,$2,$3,$4,'REGISTRATION',$5,$6,$7,$8)
                RETURNING *
                """,
                tid, fmt.value, guild_id, name, is_open, channel_id,
                created_by, is_test,
            )
        return _tournament_from_row(row)

    async def get_tournament(self, tid: str) -> Optional[UltTournament]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM ult2_tournaments WHERE id=$1", tid
            )
        return _tournament_from_row(r) if r else None

    async def get_active_tournament(
        self, guild_id: int, fmt: Format
    ) -> Optional[UltTournament]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                """
                SELECT * FROM ult2_tournaments
                WHERE guild_id=$1 AND format=$2
                  AND status = ANY($3::text[])
                ORDER BY created_at DESC LIMIT 1
                """,
                guild_id, fmt.value, list(ACTIVE_STATUSES),
            )
        return _tournament_from_row(r) if r else None

    async def list_history(
        self, guild_id: int, fmt: Optional[Format] = None, limit: int = 10
    ) -> list[UltTournament]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if fmt is None:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ult2_tournaments
                    WHERE guild_id=$1 AND status='COMPLETED'
                    ORDER BY completed_at DESC NULLS LAST LIMIT $2
                    """,
                    guild_id, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM ult2_tournaments
                    WHERE guild_id=$1 AND format=$2 AND status='COMPLETED'
                    ORDER BY completed_at DESC NULLS LAST LIMIT $3
                    """,
                    guild_id, fmt.value, limit,
                )
        return [_tournament_from_row(r) for r in rows]

    async def list_active_all(self) -> list[UltTournament]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ult2_tournaments
                WHERE status = ANY($1::text[])
                ORDER BY created_at
                """,
                list(ACTIVE_STATUSES),
            )
        return [_tournament_from_row(r) for r in rows]

    async def set_status(
        self, tid: str, status: str,
        started: bool = False, completed: bool = False,
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ult2_tournaments SET status=$2,
                    started_at = CASE WHEN $3 THEN now() ELSE started_at END,
                    completed_at = CASE WHEN $4 THEN now() ELSE completed_at END
                WHERE id=$1
                """,
                tid, status, started, completed,
            )

    async def try_start(self, tid: str) -> bool:
        """Атомарный переход REGISTRATION -> IN_PROGRESS.
        True если именно этот вызов перевёл турнир (идемпотентный старт)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE ult2_tournaments
                SET status='IN_PROGRESS', started_at=now()
                WHERE id=$1 AND status='REGISTRATION'
                RETURNING id
                """,
                tid,
            )
        return row is not None

    async def set_message_ids(
        self, tid: str, message_id: int = 0,
        bracket_message_id: int = 0, channel_id: int = 0,
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ult2_tournaments SET
                    message_id = CASE WHEN $2>0 THEN $2 ELSE message_id END,
                    bracket_message_id = CASE WHEN $3>0 THEN $3 ELSE bracket_message_id END,
                    channel_id = CASE WHEN $4>0 THEN $4 ELSE channel_id END
                WHERE id=$1
                """,
                tid, message_id, bracket_message_id, channel_id,
            )

    async def claim_bracket_message(
        self, tid: str, new_message_id: int, expected_old: int,
    ) -> bool:
        """CAS-claim bracket_message_id: True, если запись
        сменилась именно с expected_old на new_message_id.

        Используется при создании сообщения сетки: два
        параллельных resume/refresh не должны оставить
        дубликаты — проигравший удаляет своё сообщение.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE ult2_tournaments
                SET bracket_message_id=$2
                WHERE id=$1 AND bracket_message_id=$3
                RETURNING id
                """,
                tid, new_message_id, expected_old,
            )
        return row is not None

    async def set_is_test(self, tid: str, is_test: bool) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ult2_tournaments SET is_test=$2 WHERE id=$1",
                tid, is_test,
            )

    async def set_open(self, tid: str, is_open: bool) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ult2_tournaments SET is_open=$2 WHERE id=$1",
                tid, is_open,
            )

    async def delete_tournament(self, tid: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM ult2_tournaments WHERE id=$1", tid
            )

    # ---------------- УЧАСТНИКИ ----------------

    async def add_entrant(
        self, tid: str, member_ids: list[int], member_names: list[str],
        seed: Optional[int] = None, conn: Optional[asyncpg.Connection] = None,
    ) -> int:
        async def _ins(c):
            return await c.fetchval(
                """
                INSERT INTO ult2_entrants
                    (tournament_id, seed, member_ids, member_names)
                VALUES ($1,$2,$3,$4::jsonb) RETURNING id
                """,
                tid, seed, member_ids, json.dumps(member_names),
            )
        if conn is not None:
            return await _ins(conn)
        pool = await get_pool()
        async with pool.acquire() as c:
            return await _ins(c)

    async def get_entrants(self, tid: str) -> list[UltEntrant]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ult2_entrants WHERE tournament_id=$1 ORDER BY id",
                tid,
            )
        return [_entrant_from_row(r) for r in rows]

    async def get_entrant(self, eid: int) -> Optional[UltEntrant]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM ult2_entrants WHERE id=$1", eid
            )
        return _entrant_from_row(r) if r else None

    async def find_entrant_by_member(
        self, tid: str, user_id: int
    ) -> Optional[UltEntrant]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                """
                SELECT * FROM ult2_entrants
                WHERE tournament_id=$1 AND $2 = ANY(member_ids)
                """,
                tid, user_id,
            )
        return _entrant_from_row(r) if r else None

    async def remove_entrant(self, eid: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM ult2_entrants WHERE id=$1", eid)

    async def update_entrant_seed(self, eid: int, seed: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ult2_entrants SET seed=$2 WHERE id=$1", eid, seed
            )

    async def set_entrant_result(
        self, eid: int, wins: int, losses: int, status: str,
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ult2_entrants
                SET wins=$2, losses=$3, status=$4
                WHERE id=$1
                """,
                eid, wins, losses, status,
            )

    async def set_placements(
        self, tid: str, placements: dict[int, tuple[Optional[int], Optional[str]]],
    ) -> None:
        """placements: entrant_id -> (final_place, place_group)."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for eid, (place, group) in placements.items():
                    await conn.execute(
                        """
                        UPDATE ult2_entrants
                        SET final_place=$2, place_group=$3, status=$4
                        WHERE id=$1
                        """,
                        eid, place, group,
                        "CHAMPION" if place == 1 else
                        "RUNNER_UP" if place == 2 else
                        ("ELIMINATED" if group else "ACTIVE"),
                    )

    # ---------------- МАТЧИ ----------------

    async def create_match(
        self,
        tid: str, key: str, bracket: str, round_name: str,
        a_src: Slot, b_src: Slot, gf_part: int = 0,
        conn: Optional[asyncpg.Connection] = None,
    ) -> int:
        async def _ins(c):
            return await c.fetchval(
                """
                INSERT INTO ult2_matches
                    (tournament_id, key, bracket, round_name, gf_part,
                     status, side_a_src, side_b_src)
                VALUES ($1,$2,$3,$4,$5,'WAITING',$6,$7)
                RETURNING id
                """,
                tid, key, bracket, round_name, gf_part,
                slot_to_str(a_src), slot_to_str(b_src),
            )
        if conn is not None:
            return await _ins(conn)
        pool = await get_pool()
        async with pool.acquire() as c:
            return await _ins(c)

    async def get_matches(self, tid: str) -> list[UltMatch]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM ult2_matches
                WHERE tournament_id=$1
                ORDER BY
                    CASE bracket WHEN 'W' THEN 0 WHEN 'L' THEN 1 ELSE 2 END,
                    key
                """,
                tid,
            )
        return [_match_from_row(r) for r in rows]

    async def get_match(self, match_db_id: int) -> Optional[UltMatch]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM ult2_matches WHERE id=$1", match_db_id
            )
        return _match_from_row(r) if r else None

    async def get_match_by_key(
        self, tid: str, key: str
    ) -> Optional[UltMatch]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM ult2_matches WHERE tournament_id=$1 AND key=$2",
                tid, key,
            )
        return _match_from_row(r) if r else None

    async def update_match_slots(
        self, match_db_id: int,
        entrant_a: Optional[int], entrant_b: Optional[int],
        status: Optional[str] = None,
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ult2_matches SET
                    entrant_a = COALESCE($2, entrant_a),
                    entrant_b = COALESCE($3, entrant_b),
                    status = COALESCE($4, status)
                WHERE id=$1
                """,
                match_db_id, entrant_a, entrant_b, status,
            )

    async def set_match_status(self, match_db_id: int, status: str) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ult2_matches SET status=$2 WHERE id=$1",
                match_db_id, status,
            )

    async def set_room(
        self, match_db_id: int, room_id: str, room_password: str,
        updated_by: int,
    ) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ult2_matches SET
                    room_id=$2, room_password=$3,
                    room_updated_by=$4, room_updated_at=now()
                WHERE id=$1 AND status NOT IN ('COMPLETED','CANCELLED')
                """,
                match_db_id, room_id, room_password, updated_by,
            )

    # ---------------- ГОЛОСА ----------------

    async def upsert_vote(
        self, match_db_id: int, user_id: int,
        voter_entrant: Optional[int], voted_entrant: int,
        conn: Optional[asyncpg.Connection] = None,
    ) -> None:
        async def _ins(c):
            await c.execute(
                """
                INSERT INTO ult2_votes
                    (match_id, user_id, voter_entrant, voted_entrant)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (match_id, user_id)
                DO UPDATE SET voted_entrant=$4, voter_entrant=$3
                """,
                match_db_id, user_id, voter_entrant, voted_entrant,
            )
        if conn is not None:
            await _ins(conn)
            return
        pool = await get_pool()
        async with pool.acquire() as c:
            await _ins(c)

    async def get_votes(self, match_db_id: int) -> list[UltVote]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ult2_votes WHERE match_id=$1 ORDER BY id",
                match_db_id,
            )
        return [
            UltVote(
                id=r["id"], match_id=r["match_id"], user_id=r["user_id"],
                voter_entrant=r["voter_entrant"], voted_entrant=r["voted_entrant"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def clear_votes(self, match_db_id: int) -> None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM ult2_votes WHERE match_id=$1", match_db_id
            )

    # ---------------- ДУЭТЫ ----------------

    async def create_invite(
        self, tid: str, captain_id: int, partner_id: int,
        partner_name: str, ttl_seconds: int = 900,
    ) -> int:
        from datetime import timedelta
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO ult2_duo_invites
                    (tournament_id, captain_id, partner_id, partner_name,
                     status, expires_at)
                VALUES ($1,$2,$3,$4,'pending', now() + ($5 || ' seconds')::interval)
                RETURNING id
                """,
                tid, captain_id, partner_id, partner_name, str(ttl_seconds),
            )

    async def get_invite(self, invite_id: int) -> Optional[DuoInvite]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT * FROM ult2_duo_invites WHERE id=$1", invite_id
            )
        return self._invite_from_row(r) if r else None

    async def get_pending_invite_for(
        self, tid: str, user_id: int
    ) -> Optional[DuoInvite]:
        """Активное приглашение, где user — капитан или партнёр."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                """
                SELECT * FROM ult2_duo_invites
                WHERE tournament_id=$1 AND status='pending'
                  AND expires_at > now()
                  AND (captain_id=$2 OR partner_id=$2)
                ORDER BY id DESC LIMIT 1
                """,
                tid, user_id,
            )
        return self._invite_from_row(r) if r else None

    async def list_invites(self, tid: str) -> list[DuoInvite]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ult2_duo_invites WHERE tournament_id=$1 ORDER BY id",
                tid,
            )
        return [self._invite_from_row(r) for r in rows]

    async def set_invite_status(
        self, invite_id: int, status: str, message_id: int = 0,
        conn: Optional[asyncpg.Connection] = None,
    ) -> bool:
        async def _upd(c):
            row = await c.fetchrow(
                """
                UPDATE ult2_duo_invites SET status=$2,
                    message_id = CASE WHEN $3>0 THEN $3 ELSE message_id END
                WHERE id=$1 AND status='pending'
                RETURNING id
                """,
                invite_id, status, message_id,
            )
            return row is not None
        if conn is not None:
            return await _upd(conn)
        pool = await get_pool()
        async with pool.acquire() as c:
            return await _upd(c)

    async def expire_invites(self, tid: str) -> int:
        pool = await get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                """
                UPDATE ult2_duo_invites SET status='expired'
                WHERE tournament_id=$1 AND status='pending'
                  AND expires_at <= now()
                """,
                tid,
            )
        # "UPDATE n"
        try:
            return int(res.split()[-1])
        except Exception:
            return 0

    @staticmethod
    def _invite_from_row(r) -> DuoInvite:
        return DuoInvite(
            r["id"], r["tournament_id"],
            r["captain_id"], r["partner_id"], r["partner_name"],
            r["status"], r["message_id"], r["expires_at"], r["created_at"],
        )

    # ---------------- АУДИТ ----------------

    async def log(
        self, tid: str, actor_id: int, action: str,
        details: Optional[dict] = None,
        conn: Optional[asyncpg.Connection] = None,
    ) -> None:
        import json as _json
        payload = _json.dumps(details) if details is not None else None
        async def _ins(c):
            await c.execute(
                """
                INSERT INTO ult2_audit (tournament_id, actor_id, action, details)
                VALUES ($1,$2,$3,$4::jsonb)
                """,
                tid, actor_id, action, payload,
            )
        if conn is not None:
            await _ins(conn)
            return
        pool = await get_pool()
        async with pool.acquire() as c:
            await _ins(c)

    async def get_audit(self, tid: str, limit: int = 50) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ult2_audit WHERE tournament_id=$1 ORDER BY id DESC LIMIT $2",
                tid, limit,
            )
        return [dict(r) for r in rows]


ultimatum_store = UltimatumStore()
