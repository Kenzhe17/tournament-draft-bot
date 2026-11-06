"""BrService — Battle Royale лобби (порт br-service.js).

N команд, несколько игр, очки = placement_points[placement] + kills*kill_points
+ adjustments. Standings считаются на лету.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from arbiter.enums import MatchStatus
from arbiter.models import (
    BrAdjustment, BrGameResult, BrLobby, BrLog, BrTeam,
    dumps, loads, new_id, utcnow,
)
from arbiter.services.org_service import org_service
from storage.db import get_pool

logger = logging.getLogger(__name__)

DEFAULT_PLACEMENT_POINTS = [10, 8, 6, 5, 4, 3, 2, 1]  # места 1..8


class BrError(Exception):
    pass


def _lobby(r: asyncpg.Record) -> BrLobby:
    return BrLobby(
        id=r["id"], public_code=r["public_code"], organization_id=r["organization_id"],
        channel_id=r["channel_id"], control_message_id=r["control_message_id"],
        created_by_id=r["created_by_id"], name=r["name"], game=r["game"],
        status=MatchStatus(r["status"]), games_planned=r["games_planned"],
        kill_points=r["kill_points"], placement_points=loads(r["placement_points"]),
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


def _team(r: asyncpg.Record) -> BrTeam:
    return BrTeam(
        id=r["id"], lobby_id=r["lobby_id"], linked_team_id=r["linked_team_id"],
        name=r["name"], seed=r["seed"], discord_role_id=r["discord_role_id"],
        category_channel_id=r["category_channel_id"], text_channel_id=r["text_channel_id"],
        voice_channel_id=r["voice_channel_id"], team_message_id=r["team_message_id"],
        created_at=r["created_at"],
    )


class BrService:
    # ---------- lobbies ----------

    async def create_lobby(self, org_id: str, name: str, game: str, *,
                           games_planned: int = 6, kill_points: int = 1,
                           placement_points: list | None = None,
                           channel_id: str | None = None,
                           created_by_id: str | None = None) -> BrLobby:
        lobby = BrLobby(
            organization_id=org_id, name=name.strip(), game=game.strip(),
            games_planned=games_planned, kill_points=kill_points,
            placement_points=placement_points or DEFAULT_PLACEMENT_POINTS,
            channel_id=channel_id, created_by_id=created_by_id,
        )
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO arb_br_lobbies (id, public_code, organization_id, channel_id, "
                "created_by_id, name, game, status, games_planned, kill_points, "
                "placement_points, created_at, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                lobby.id, lobby.public_code, org_id, channel_id, created_by_id,
                lobby.name, lobby.game, MatchStatus.PENDING.value,
                games_planned, kill_points, dumps(lobby.placement_points),
                utcnow(), utcnow())
            await org_service._audit(conn, org_id, created_by_id, "br.create",
                                     "br_lobby", lobby.id,
                                     {"name": lobby.name, "game": lobby.game})
        return lobby

    async def get_lobby(self, lobby_id: str) -> BrLobby | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_br_lobbies WHERE id=$1 OR public_code=$1", lobby_id)
            return _lobby(row) if row else None

    async def list_lobbies(self, org_id: str) -> list[BrLobby]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_br_lobbies WHERE organization_id=$1 "
                "AND status NOT IN ('COMPLETE','CANCELLED') ORDER BY created_at DESC",
                org_id)
            return [_lobby(r) for r in rows]

    async def set_status(self, lobby: BrLobby, status: MatchStatus,
                         actor_id: str | None = None) -> BrLobby:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE arb_br_lobbies SET status=$1, updated_at=$2 WHERE id=$3 RETURNING *",
                status.value, utcnow(), lobby.id)
            await org_service._audit(conn, lobby.organization_id, actor_id,
                                     "br.status", "br_lobby", lobby.id,
                                     {"status": status.value})
        return _lobby(row)

    # ---------- teams ----------

    async def add_team(self, lobby_id: str, name: str, *,
                       linked_team_id: str | None = None, seed: int | None = None) -> BrTeam:
        pool = await get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM arb_br_teams WHERE lobby_id=$1", lobby_id)
            team = BrTeam(lobby_id=lobby_id, name=name.strip(),
                          linked_team_id=linked_team_id, seed=seed or count + 1)
            await conn.execute(
                "INSERT INTO arb_br_teams (id, lobby_id, linked_team_id, name, seed, "
                "created_at) VALUES ($1,$2,$3,$4,$5,$6)",
                team.id, lobby_id, linked_team_id, team.name, team.seed, utcnow())
        return team

    async def list_teams(self, lobby_id: str) -> list[BrTeam]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_br_teams WHERE lobby_id=$1 ORDER BY seed NULLS LAST, name",
                lobby_id)
            return [_team(r) for r in rows]

    async def get_team(self, lobby_id: str, team_name: str) -> BrTeam | None:
        teams = await self.list_teams(lobby_id)
        for t in teams:
            if t.name.lower() == team_name.lower():
                return t
        return None

    # ---------- results ----------

    def _points_for(self, lobby: BrLobby, placement: int, kills: int) -> int:
        pp = lobby.placement_points or DEFAULT_PLACEMENT_POINTS
        base = pp[placement - 1] if 1 <= placement <= len(pp) else 0
        return base + kills * lobby.kill_points

    async def log_game(self, lobby: BrLobby, team_name: str, game_number: int,
                       placement: int, kills: int, *,
                       actor_id: str | None = None) -> BrGameResult:
        """Записать результат команды в игре (placement + kills)."""
        team = await self.get_team(lobby.id, team_name)
        if not team:
            raise BrError(f"Команда '{team_name}' не найдена в лобби")
        if game_number < 1 or game_number > lobby.games_planned:
            raise BrError(f"Номер игры: 1–{lobby.games_planned}")
        if placement < 1 or placement > 100:
            raise BrError("Placement: 1–100")
        points = self._points_for(lobby, placement, kills)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO arb_br_game_results (id, lobby_id, br_team_id, game_number, "
                    "placement, kills, points, actor_id, created_at, updated_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
                    "ON CONFLICT (lobby_id, br_team_id, game_number) DO UPDATE SET "
                    "placement=$5, kills=$6, points=$7, actor_id=$8, updated_at=$10 "
                    "RETURNING *",
                    new_id(), lobby.id, team.id, game_number, placement, kills,
                    points, actor_id, utcnow(), utcnow())
                await org_service._audit(conn, lobby.organization_id, actor_id,
                                         "br.log_game", "br_lobby", lobby.id,
                                         {"team": team_name, "game": game_number,
                                          "placement": placement, "kills": kills})
        return BrGameResult(
            id=row["id"], lobby_id=row["lobby_id"], br_team_id=row["br_team_id"],
            game_number=row["game_number"], placement=row["placement"],
            kills=row["kills"], points=row["points"], actor_id=row["actor_id"],
            created_at=row["created_at"], updated_at=row["updated_at"])

    # ---------- adjustments ----------

    async def add_adjustment(self, lobby_id: str, team_name: str, points: int,
                             *, kills: int = 0, game_number: int | None = None,
                             reason: str = "", actor_id: str | None = None) -> str:
        lobby = await self.get_lobby(lobby_id)
        if not lobby:
            raise BrError("Лобби не найдено")
        team = await self.get_team(lobby_id, team_name)
        if not team:
            raise BrError(f"Команда '{team_name}' не найдена")
        pool = await get_pool()
        async with pool.acquire() as conn:
            aid = new_id()
            await conn.execute(
                "INSERT INTO arb_br_adjustments (id, lobby_id, br_team_id, points, kills, "
                "game_number, reason, actor_id, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                aid, lobby_id, team.id, points, kills, game_number, reason,
                actor_id, utcnow())
            await org_service._audit(conn, lobby.organization_id, actor_id,
                                     "br.adjust", "br_lobby", lobby_id,
                                     {"team": team_name, "points": points, "reason": reason})
        return aid

    # ---------- logs ----------

    async def add_log(self, lobby_id: str, kind: str, *, br_team_id: str | None = None,
                      subject: str | None = None, game_number: int | None = None,
                      summary: str | None = None, details: str | None = None,
                      rule: str | None = None, duration_minutes: int | None = None,
                      attachment_url: str | None = None, attachment_name: str | None = None,
                      actor_id: str | None = None) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            lid = new_id()
            await conn.execute(
                "INSERT INTO arb_br_logs (id, lobby_id, kind, br_team_id, subject, "
                "game_number, summary, details, rule, duration_minutes, attachment_url, "
                "attachment_name, actor_id, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,"
                "$10,$11,$12,$13,$14)",
                lid, lobby_id, kind, br_team_id, subject, game_number, summary, details,
                rule, duration_minutes, attachment_url, attachment_name, actor_id, utcnow())
            await org_service._audit(conn, None, actor_id, "br.log", "br_lobby", lobby_id,
                                     {"kind": kind, "summary": summary})
        return lid

    async def list_logs(self, lobby_id: str, limit: int = 30) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_br_logs WHERE lobby_id=$1 ORDER BY created_at DESC LIMIT $2",
                lobby_id, limit)
            return [dict(r) for r in rows]

    # ---------- standings ----------

    async def standings(self, lobby_id: str) -> list[dict]:
        """Очки = game results + adjustments."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            teams = await conn.fetch(
                "SELECT * FROM arb_br_teams WHERE lobby_id=$1 ORDER BY seed NULLS LAST, name",
                lobby_id)
            results = await conn.fetch(
                "SELECT br_team_id, game_number, placement, kills, points "
                "FROM arb_br_game_results WHERE lobby_id=$1", lobby_id)
            adj = await conn.fetch(
                "SELECT br_team_id, SUM(points) AS p, SUM(kills) AS k "
                "FROM arb_br_adjustments WHERE lobby_id=$1 GROUP BY br_team_id", lobby_id)
        res_by_team: dict[str, list] = {}
        for r in results:
            res_by_team.setdefault(r["br_team_id"], []).append(r)
        adj_by_team = {a["br_team_id"]: a for a in adj}

        table = []
        for t in teams:
            games = res_by_team.get(t["id"], [])
            game_points = sum(g["points"] for g in games)
            game_kills = sum(g["kills"] for g in games)
            a = adj_by_team.get(t["id"])
            adj_points = a["p"] if a else 0
            adj_kills = a["k"] if a else 0
            table.append({
                "name": t["name"], "seed": t["seed"],
                "games_played": len(games),
                "total_points": game_points + adj_points,
                "kills": game_kills + adj_kills,
                "avg_placement": (sum(g["placement"] for g in games) / len(games))
                if games else None,
            })
        table.sort(key=lambda x: (-x["total_points"], x["avg_placement"] or 999))
        return table

    async def finalize(self, lobby: BrLobby, actor_id: str | None = None) -> BrLobby:
        return await self.set_status(lobby, MatchStatus.COMPLETE, actor_id)


br_service = BrService()
