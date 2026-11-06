"""MatchService — жизненный цикл матчей Arbiter.

Порт логики src/services/match-service.js: создание, veto, счёт по картам,
паузы, варны, доказательства, рулинги, закрытие, аудит.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg
import discord

from arbiter.enums import MatchStatus, VetoKind, VetoMode
from arbiter.models import (
    CharacterBanAction,
    MapResult,
    Match,
    MatchRefereeAssignment,
    VetoAction,
    dumps,
    loads,
    new_id,
    utcnow,
)
from arbiter.services.org_service import OrgService, org_service
from storage.db import get_pool

logger = logging.getLogger(__name__)


class MatchError(Exception):
    pass


def _row_to_match(r: asyncpg.Record) -> Match:
    return Match(
        id=r["id"], public_code=r["public_code"], organization_id=r["organization_id"],
        tournament_id=r["tournament_id"], channel_id=r["channel_id"],
        control_message_id=r["control_message_id"], created_by_id=r["created_by_id"],
        team_a_name=r["team_a_name"], team_b_name=r["team_b_name"],
        best_of=r["best_of"], rules_preset=r["rules_preset"],
        veto_mode=VetoMode(r["veto_mode"]) if r["veto_mode"] else VetoMode.FINAL_MAP_BAN,
        veto_starting_team=r["veto_starting_team"] or "teamA",
        veto_timer_seconds=r["veto_timer_seconds"] or 60,
        veto_timeout_action=r["veto_timeout_action"] or "referee_choice",
        character_ban_mode=r["character_ban_mode"] or "none",
        character_ban_timer_seconds=r["character_ban_timer_seconds"] or 30,
        character_ban_started_at=r["character_ban_started_at"],
        character_pool=loads(r["character_pool"]),
        map_pool=loads(r["map_pool"], []),
        status=MatchStatus(r["status"]),
        team_a_score=r["team_a_score"], team_b_score=r["team_b_score"],
        team_a_result=r["team_a_result"], team_b_result=r["team_b_result"],
        allow_player_reports=r["allow_player_reports"],
        assigned_referee_id=r["assigned_referee_id"],
        referee_claim_mode=r["referee_claim_mode"] or "open",
        referee_claim_limit=r["referee_claim_limit"] or 1,
        dispute_reason=r["dispute_reason"],
        team_a_role_id=r["team_a_role_id"], team_b_role_id=r["team_b_role_id"],
        roster_locked_at=r["roster_locked_at"], roster_locked_by_id=r["roster_locked_by_id"],
        final_map=r["final_map"], external_ref=r.get("external_ref"),
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


VALID_TRANSITIONS = {
    MatchStatus.PENDING: {MatchStatus.VETO, MatchStatus.LIVE, MatchStatus.CANCELLED},
    MatchStatus.VETO: {MatchStatus.LIVE, MatchStatus.DISPUTED, MatchStatus.CANCELLED, MatchStatus.COMPLETE},
    MatchStatus.LIVE: {MatchStatus.DISPUTED, MatchStatus.COMPLETE, MatchStatus.CANCELLED},
    MatchStatus.DISPUTED: {MatchStatus.LIVE, MatchStatus.COMPLETE, MatchStatus.CANCELLED},
}


class MatchService:
    # ---------- CRUD ----------

    async def create_match(
        self,
        org_id: str,
        team_a_name: str,
        team_b_name: str,
        *,
        best_of: int = 1,
        map_pool: list | None = None,
        rules_preset: str = "generic",
        veto_mode: VetoMode = VetoMode.FINAL_MAP_BAN,
        tournament_id: str | None = None,
        channel_id: str | None = None,
        created_by_id: str | None = None,
        referee_claim_mode: str = "open",
        referee_claim_limit: int = 1,
        allow_player_reports: bool = False,
        external_ref: str | None = None,
    ) -> Match:
        if best_of < 1 or best_of > 99:
            raise MatchError("BO должен быть от 1 до 99")
        if not team_a_name.strip() or not team_b_name.strip():
            raise MatchError("Названия команд обязательны")
        match = Match(
            organization_id=org_id, tournament_id=tournament_id,
            channel_id=channel_id, created_by_id=created_by_id,
            team_a_name=team_a_name.strip(), team_b_name=team_b_name.strip(),
            best_of=best_of, map_pool=map_pool or [], rules_preset=rules_preset,
            veto_mode=veto_mode, status=MatchStatus.PENDING,
            referee_claim_mode=referee_claim_mode,
            referee_claim_limit=referee_claim_limit,
            allow_player_reports=allow_player_reports,
            external_ref=external_ref,
        )
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO arb_matches (id, public_code, organization_id, tournament_id, "
                "channel_id, created_by_id, team_a_name, team_b_name, best_of, map_pool, "
                "rules_preset, veto_mode, status, referee_claim_mode, referee_claim_limit, "
                "allow_player_reports, external_ref, created_at, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)",
                match.id, match.public_code, org_id, tournament_id, channel_id,
                created_by_id, match.team_a_name, match.team_b_name, best_of,
                dumps(match.map_pool), rules_preset, veto_mode.value,
                MatchStatus.PENDING.value, referee_claim_mode, referee_claim_limit,
                allow_player_reports, external_ref, utcnow(), utcnow(),
            )
            await org_service._audit(conn, org_id, created_by_id, "match.create",
                                     "match", match.id,
                                     {"code": match.public_code, "a": match.team_a_name,
                                      "b": match.team_b_name, "bo": best_of})
        return match

    async def get_match(self, match_id: str) -> Match | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM arb_matches WHERE id = $1", match_id)
            return _row_to_match(row) if row else None

    async def get_match_by_code(self, code: str) -> Match | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_matches WHERE public_code = $1 OR id = $1", code
            )
            return _row_to_match(row) if row else None

    async def get_match_by_external_ref(self, external_ref: str) -> Match | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_matches WHERE external_ref = $1", external_ref
            )
            return _row_to_match(row) if row else None

    async def list_active_matches(self, org_id: str) -> list[Match]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_matches WHERE organization_id = $1 "
                "AND status NOT IN ('COMPLETE','CANCELLED') ORDER BY created_at DESC",
                org_id,
            )
            return [_row_to_match(r) for r in rows]

    async def list_matches(self, org_id: str, limit: int = 20) -> list[Match]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_matches WHERE organization_id = $1 "
                "ORDER BY created_at DESC LIMIT $2", org_id, limit,
            )
            return [_row_to_match(r) for r in rows]

    async def update_match(self, match: Match, **fields: Any) -> Match:
        allowed = {c.name for c in Match.__dataclass_fields__.values()} - {
            "id", "created_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return match
        pool = await get_pool()
        sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
        values = []
        for v in updates.values():
            if isinstance(v, (VetoMode, MatchStatus)):
                v = v.value
            elif isinstance(v, (list, dict)):
                v = dumps(v)
            values.append(v)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE arb_matches SET {sets}, updated_at = $1 WHERE id = ${len(updates)+2} "
                f"RETURNING *",
                utcnow(), *values, match.id,
            )
            if not row:
                raise MatchError("Матч не найден")
            return _row_to_match(row)

    async def transition(self, match: Match, new_status: MatchStatus,
                         actor_id: str | None = None) -> Match:
        """Переход статуса с валидацией state machine."""
        allowed = VALID_TRANSITIONS.get(match.status, set())
        if match.status in (MatchStatus.COMPLETE, MatchStatus.CANCELLED):
            raise MatchError(f"Матч уже {match.status.value}")
        if new_status not in allowed and not (
            match.status == MatchStatus.DISPUTED and new_status == MatchStatus.COMPLETE
        ):
            raise MatchError(f"Недопустимый переход {match.status.value} → {new_status.value}")
        updated = await self.update_match(match, status=new_status)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await org_service._audit(conn, match.organization_id, actor_id,
                                     "match.status", "match", match.id,
                                     {"from": match.status.value, "to": new_status.value})
        return updated

    # ---------- veto ----------

    async def list_map_pool(self, match: Match) -> list[str]:
        if match.map_pool:
            return [str(m) for m in match.map_pool]
        # Дефолтный пул, если пресет не задан
        return ["Map 1", "Map 2", "Map 3", "Map 4", "Map 5"]

    async def add_veto_action(self, match: Match, kind: VetoKind, team_slot: str,
                              map_name: str, *, source: str = "team",
                              note: str | None = None, actor_id: str | None = None) -> VetoAction:
        if team_slot not in ("teamA", "teamB"):
            raise MatchError("team_slot должен быть teamA или teamB")
        pool_names = await self.list_map_pool(match)
        if map_name not in pool_names:
            raise MatchError(f"Карта '{map_name}' не в пуле карты")
        action = VetoAction(match_id=match.id, kind=kind, team_slot=team_slot,
                            map_name=map_name, source=source, note=note, actor_id=actor_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO arb_veto_actions (id, match_id, kind, team_slot, map_name, "
                "source, note, actor_id, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                action.id, match.id, kind.value, team_slot, map_name, source, note,
                actor_id, utcnow(),
            )
            await org_service._audit(conn, match.organization_id, actor_id,
                                     "match.veto", "match", match.id,
                                     {"kind": kind.value, "slot": team_slot, "map": map_name})
        return action

    async def list_veto_actions(self, match_id: str) -> list[VetoAction]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_veto_actions WHERE match_id = $1 ORDER BY created_at",
                match_id,
            )
        return [
            VetoAction(id=r["id"], match_id=r["match_id"], kind=VetoKind(r["kind"]),
                       team_slot=r["team_slot"], map_name=r["map_name"],
                       source=r["source"], note=r["note"], actor_id=r["actor_id"],
                       created_at=r["created_at"])
            for r in rows
        ]

    async def available_maps(self, match: Match) -> list[str]:
        """Оставшиеся карты после veto/банов."""
        vetoes = await self.list_veto_actions(match.id)
        pool = await self.list_map_pool(match)
        used = {v.map_name for v in vetoes if v.kind == VetoKind.BAN}
        return [m for m in pool if m not in used]

    # ---------- map results ----------

    async def record_map_result(
        self, match: Match, map_index: int, map_name: str,
        team_a_score: int, team_b_score: int,
        *, team_a_result: str | None = None, team_b_result: str | None = None,
        note: str | None = None, actor_id: str | None = None,
    ) -> MapResult:
        if not match.is_active:
            raise MatchError("Матч не активен")
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT id FROM arb_map_results WHERE match_id = $1 AND map_index = $2",
                    match.id, map_index,
                )
                if existing:
                    row = await conn.fetchrow(
                        "UPDATE arb_map_results SET map_name=$1, team_a_score=$2, "
                        "team_b_score=$3, team_a_result=$4, team_b_result=$5, note=$6, "
                        "actor_id=$7, updated_at=$8 WHERE id=$9 RETURNING *",
                        map_name, team_a_score, team_b_score, team_a_result,
                        team_b_result, note, actor_id, utcnow(), existing["id"],
                    )
                else:
                    row = await conn.fetchrow(
                        "INSERT INTO arb_map_results (id, match_id, map_index, map_name, "
                        "team_a_score, team_b_score, team_a_result, team_b_result, "
                        "status, note, actor_id, created_at, updated_at) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *",
                        new_id(), match.id, map_index, map_name, team_a_score,
                        team_b_score, team_a_result, team_b_result, "complete",
                        note, actor_id, utcnow(), utcnow(),
                    )
                await org_service._audit(conn, match.organization_id, actor_id,
                                         "match.map_result", "match", match.id,
                                         {"map_index": map_index, "map": map_name,
                                          "a": team_a_score, "b": team_b_score})
        return MapResult(
            id=row["id"], match_id=row["match_id"], map_index=row["map_index"],
            map_name=row["map_name"], team_a_score=row["team_a_score"],
            team_b_score=row["team_b_score"], team_a_result=row["team_a_result"],
            team_b_result=row["team_b_result"], status=row["status"],
            note=row["note"], actor_id=row["actor_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def list_map_results(self, match_id: str) -> list[MapResult]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_map_results WHERE match_id = $1 ORDER BY map_index",
                match_id,
            )
        return [
            MapResult(id=r["id"], match_id=r["match_id"], map_index=r["map_index"],
                      map_name=r["map_name"], team_a_score=r["team_a_score"],
                      team_b_score=r["team_b_score"], team_a_result=r["team_a_result"],
                      team_b_result=r["team_b_result"], status=r["status"],
                      note=r["note"], actor_id=r["actor_id"],
                      created_at=r["created_at"], updated_at=r["updated_at"])
            for r in rows
        ]

    # ---------- score ----------

    async def set_score(self, match: Match, team_a_score: int, team_b_score: int,
                        *, team_a_result: str | None = None,
                        team_b_result: str | None = None,
                        actor_id: str | None = None) -> Match:
        if team_a_score < 0 or team_b_score < 0:
            raise MatchError("Счёт не может быть отрицательным")
        needed = match.maps_to_win
        if max(team_a_score, team_b_score) > needed:
            raise MatchError(f"Счёт не может превышать {needed} (BO{match.best_of})")
        updated = await self.update_match(
            match, team_a_score=team_a_score, team_b_score=team_b_score,
            team_a_result=team_a_result, team_b_result=team_b_result,
        )
        pool = await get_pool()
        async with pool.acquire() as conn:
            await org_service._audit(conn, match.organization_id, actor_id,
                                     "match.score", "match", match.id,
                                     {"a": team_a_score, "b": team_b_score})
        return updated

    async def winner(self, match: Match) -> str | None:
        """'teamA' | 'teamB' | None."""
        if match.team_a_score >= match.maps_to_win:
            return "teamA"
        if match.team_b_score >= match.maps_to_win:
            return "teamB"
        return None

    # ---------- complete / cancel ----------

    async def complete_match(self, match: Match, *, winner_slot: str | None = None,
                             result_a: str | None = None, result_b: str | None = None,
                             final_map: str | None = None,
                             actor_id: str | None = None) -> Match:
        """Финализировать матч. winner_slot: 'teamA'|'teamB'|None (ничья)."""
        if match.status in (MatchStatus.COMPLETE, MatchStatus.CANCELLED):
            raise MatchError("Матч уже завершён")
        if winner_slot and winner_slot not in ("teamA", "teamB"):
            raise MatchError("winner_slot: teamA или teamB")
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE arb_matches SET status=$1, team_a_result=$2, team_b_result=$3, "
                    "final_map=$4, updated_at=$5 WHERE id=$6 RETURNING *",
                    MatchStatus.COMPLETE.value, result_a, result_b, final_map,
                    utcnow(), match.id,
                )
                await org_service._audit(conn, match.organization_id, actor_id,
                                         "match.complete", "match", match.id,
                                         {"winner": winner_slot, "a_result": result_a,
                                          "b_result": result_b, "final_map": final_map})
        # Продвижение в сетке турнира (если матч привязан к bracket node)
        try:
            from arbiter.services.bracket_service import bracket_service
            node = await self._find_node_by_match(match.id)
            if node and node.status != "complete":
                await bracket_service.complete_node(
                    node.id, winner_slot or "teamA",
                    team_a_score=match.team_a_score, team_b_score=match.team_b_score,
                    team_a_result=result_a, team_b_result=result_b)
        except Exception as e:
            logger.error("Bracket progression failed for match %s: %s", match.id, e)
        return _row_to_match(row)

    async def _find_node_by_match(self, match_id: str):
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM arb_bracket_nodes WHERE match_id=$1", match_id)
            if row:
                from arbiter.services.bracket_service import bracket_service
                return await bracket_service.get_node(row["id"])
            return None

    async def cancel_match(self, match: Match, *, reason: str = "",
                           actor_id: str | None = None) -> Match:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE arb_matches SET status=$1, dispute_reason=$2, updated_at=$3 "
                    "WHERE id=$4 RETURNING *",
                    MatchStatus.CANCELLED.value, reason or None, utcnow(), match.id,
                )
                await org_service._audit(conn, match.organization_id, actor_id,
                                         "match.cancel", "match", match.id,
                                         {"reason": reason})
        return _row_to_match(row)

    async def dispute_match(self, match: Match, reason: str,
                            actor_id: str | None = None) -> Match:
        updated = await self.transition(match, MatchStatus.DISPUTED, actor_id)
        return await self.update_match(updated, dispute_reason=reason)

    # ---------- referees ----------

    async def assign_referee(self, match: Match, discord_user_id: str | int,
                             *, source: str = "assigned",
                             assigned_by_id: str | None = None) -> MatchRefereeAssignment:
        profile = await org_service.get_or_create_profile_by_id(discord_user_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE arb_referee_assignments SET status='released', updated_at=$1 "
                    "WHERE match_id=$2 AND status='active'",
                    utcnow(), match.id,
                )
                row = await conn.fetchrow(
                    "INSERT INTO arb_referee_assignments (id, match_id, user_profile_id, "
                    "source, status, assigned_by_id, created_at, updated_at) "
                    "VALUES ($1,$2,$3,$4,'active',$5,$6,$7) RETURNING *",
                    new_id(), match.id, profile.id, source, assigned_by_id,
                    utcnow(), utcnow(),
                )
                await conn.execute(
                    "UPDATE arb_matches SET assigned_referee_id=$1, updated_at=$2 WHERE id=$3",
                    profile.id, utcnow(), match.id,
                )
                await org_service._audit(conn, match.organization_id, assigned_by_id,
                                         "match.ref_assign", "match", match.id,
                                         {"referee": profile.discord_user_id, "source": source})
        return MatchRefereeAssignment(
            id=row["id"], match_id=row["match_id"], user_profile_id=row["user_profile_id"],
            source=row["source"], status=row["status"],
            assigned_by_id=row["assigned_by_id"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def claim_referee(self, match: Match, discord_user_id: str | int) -> MatchRefereeAssignment:
        """Claim матча судьёй (если claim_mode позволяет)."""
        if match.referee_claim_mode == "assigned":
            raise MatchError("Матч требует назначенного судью")
        active = await self.list_active_refs(match.id)
        if len(active) >= match.referee_claim_limit:
            raise MatchError("Лимит судей на матч исчерпан")
        profile = await org_service.get_or_create_profile_by_id(discord_user_id)
        if any(a.user_profile_id == profile.id for a in active):
            raise MatchError("Вы уже судья этого матча")
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO arb_referee_assignments (id, match_id, user_profile_id, "
                "source, status, created_at, updated_at) VALUES ($1,$2,$3,'claimed','active',$4,$5) "
                "RETURNING *",
                new_id(), match.id, profile.id, utcnow(), utcnow(),
            )
            await org_service._audit(conn, match.organization_id, profile.id,
                                     "match.ref_claim", "match", match.id, {})
        return MatchRefereeAssignment(
            id=row["id"], match_id=row["match_id"], user_profile_id=row["user_profile_id"],
            source="claimed", status="active",
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def release_referee(self, match_id: str, discord_user_id: str | int) -> bool:
        profile = await org_service.get_profile_by_discord_id(discord_user_id)
        if not profile:
            return False
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE arb_referee_assignments SET status='released', updated_at=$1 "
                "WHERE match_id=$2 AND user_profile_id=$3 AND status='active' RETURNING id",
                utcnow(), match_id, profile.id,
            )
            return row is not None

    async def list_active_refs(self, match_id: str) -> list[MatchRefereeAssignment]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_referee_assignments WHERE match_id=$1 AND status='active'",
                match_id,
            )
        return [
            MatchRefereeAssignment(id=r["id"], match_id=r["match_id"],
                                   user_profile_id=r["user_profile_id"], source=r["source"],
                                   status=r["status"], assigned_by_id=r["assigned_by_id"],
                                   created_at=r["created_at"], updated_at=r["updated_at"])
            for r in rows
        ]

    async def is_referee_of(self, match_id: str, discord_user_id: str | int) -> bool:
        profile = await org_service.get_profile_by_discord_id(discord_user_id)
        if not profile:
            return False
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM arb_referee_assignments "
                "WHERE match_id=$1 AND user_profile_id=$2 AND status='active')",
                match_id, profile.id,
            ) or False

    # ---------- panels ----------

    async def set_control_message(self, match: Match, channel_id: str,
                                  message_id: str) -> Match:
        return await self.update_match(
            match, channel_id=channel_id, control_message_id=message_id,
        )


match_service = MatchService()
