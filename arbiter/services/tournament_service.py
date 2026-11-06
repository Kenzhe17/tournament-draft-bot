"""TournamentService — CRUD турниров и связка bracket nodes с матчами."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from arbiter.enums import ClaimMode, TournamentFormat, TournamentStatus
from arbiter.models import Tournament, TournamentEntry, dumps, loads, new_id, utcnow
from arbiter.services.org_service import org_service
from storage.db import get_pool

logger = logging.getLogger(__name__)


class TournamentError(Exception):
    pass


def _row_to_tournament(r: asyncpg.Record) -> Tournament:
    return Tournament(
        id=r["id"], public_code=r["public_code"], organization_id=r["organization_id"],
        name=r["name"], game_title=r["game_title"], format=TournamentFormat(r["format"]),
        status=TournamentStatus(r["status"]), best_of=r["best_of"],
        third_place=r["third_place"], referee_claim_mode=r["referee_claim_mode"] or "open",
        referee_claim_limit=r["referee_claim_limit"] or 1,
        champion_name=r["champion_name"], started_at=r["started_at"],
        completed_at=r["completed_at"], created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


class TournamentService:
    async def create_tournament(
        self, org_id: str, name: str, *, game_title: str | None = None,
        format: TournamentFormat = TournamentFormat.SINGLE_ELIMINATION,
        best_of: int = 1, third_place: bool = False,
        referee_claim_mode: str = "open", created_by_id: str | None = None,
    ) -> Tournament:
        t = Tournament(
            organization_id=org_id, name=name.strip(), game_title=game_title,
            format=format, best_of=best_of, third_place=third_place,
            referee_claim_mode=referee_claim_mode,
            referee_claim_limit=1 if referee_claim_mode == "open" else 1,
        )
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO arb_tournaments (id, public_code, organization_id, name, "
                "game_title, format, status, best_of, third_place, referee_claim_mode, "
                "referee_claim_limit, created_at, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                t.id, t.public_code, org_id, t.name, game_title, format.value,
                TournamentStatus.DRAFT.value, best_of, third_place,
                referee_claim_mode, 1, utcnow(), utcnow())
            await org_service._audit(conn, org_id, created_by_id, "tournament.create",
                                     "tournament", t.id, {"name": t.name, "format": format.value})
        return t

    async def get_tournament(self, tournament_id: str) -> Tournament | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_tournaments WHERE id=$1 OR public_code=$1", tournament_id)
            return _row_to_tournament(row) if row else None

    async def list_tournaments(self, org_id: str, *, active_only: bool = False) -> list[Tournament]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if active_only:
                rows = await conn.fetch(
                    "SELECT * FROM arb_tournaments WHERE organization_id=$1 "
                    "AND status IN ('draft','active') ORDER BY created_at DESC", org_id)
            else:
                rows = await conn.fetch(
                    "SELECT * FROM arb_tournaments WHERE organization_id=$1 "
                    "ORDER BY created_at DESC LIMIT 20", org_id)
            return [_row_to_tournament(r) for r in rows]

    async def update_tournament(self, tournament: Tournament, **fields: Any) -> Tournament:
        allowed = {"name", "game_title", "format", "status", "best_of", "third_place",
                   "referee_claim_mode", "champion_name", "started_at", "completed_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return tournament
        pool = await get_pool()
        sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
        values = []
        for v in updates.values():
            if isinstance(v, (TournamentFormat, TournamentStatus)):
                v = v.value
            values.append(v)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE arb_tournaments SET {sets}, updated_at=$1 WHERE id=${len(updates)+2} "
                f"RETURNING *", utcnow(), *values, tournament.id)
            if not row:
                raise TournamentError("Турнир не найден")
            return _row_to_tournament(row)

    async def activate(self, tournament: Tournament) -> Tournament:
        """draft -> active (сетка должна быть сгенерирована)."""
        if tournament.status != TournamentStatus.DRAFT:
            raise TournamentError("Турнир уже активен/завершён")
        return await self.update_tournament(
            tournament, status=TournamentStatus.ACTIVE, started_at=utcnow())

    # ---------- node -> match ----------

    async def create_match_for_node(self, node_id: str, org_id: str,
                                    channel_id: str | None = None,
                                    created_by_id: str | None = None):
        """Создать матч для bracket node (когда обе команды известны)."""
        from arbiter.services.bracket_service import bracket_service
        from arbiter.services.match_service import match_service
        node = await bracket_service.get_node(node_id)
        if not node:
            raise TournamentError("Узел не найден")
        if not node.team_a_name or not node.team_b_name:
            raise TournamentError("Обе команды ещё не определены")
        if node.match_id:
            existing = await match_service.get_match(node.match_id)
            if existing and existing.is_active:
                return existing
        match = await match_service.create_match(
            org_id,
            node.team_a_name or "TBD",
            node.team_b_name or "TBD",
            best_of=node.best_of,
            tournament_id=node.tournament_id,
            channel_id=channel_id,
            created_by_id=created_by_id,
            referee_claim_mode="open",
        )
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE arb_bracket_nodes SET match_id=$1, status='live', updated_at=$2 "
                "WHERE id=$3", match.id, utcnow(), node_id)
        return match

    async def ensure_node_matches(self, tournament: Tournament, org_id: str,
                                  channel_id: str | None = None,
                                  created_by_id: str | None = None) -> list:
        """Создать матчи для всех ready/pending узлов с известными командами."""
        from arbiter.services.bracket_service import bracket_service
        nodes = await bracket_service.list_nodes(tournament.id)
        created = []
        for node in nodes:
            if node.match_id or node.status in ("complete", "bye"):
                continue
            if node.team_a_name and node.team_b_name:
                try:
                    m = await self.create_match_for_node(
                        node.id, org_id, channel_id, created_by_id)
                    created.append(m)
                except Exception as e:
                    logger.warning("Failed to create match for node %s: %s", node.id, e)
        return created


tournament_service = TournamentService()
