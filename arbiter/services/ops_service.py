"""OpsService — операционные workflow Arbiter:
evidence, score reports, roster, checkin, rulebook, shifts,
warnings, pauses, announcements, timeline.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from arbiter.enums import EvidenceStatus, RosterStatus, ScoreReportStatus
from arbiter.models import dumps, loads, new_id, utcnow
from arbiter.services.org_service import org_service
from storage.db import get_pool

logger = logging.getLogger(__name__)


class OpsError(Exception):
    pass


class OpsService:
    # ---------- pause logs ----------

    async def add_pause(self, org_id: str, match_id: str, duration_minutes: int,
                        reason: str, *, pause_type: str = "team",
                        team_name: str | None = None, actor_id: str | None = None) -> str:
        if duration_minutes <= 0 or duration_minutes > 24 * 60:
            raise OpsError("Длительность паузы: 1–1440 минут")
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                pid = new_id()
                await conn.execute(
                    "INSERT INTO arb_pause_logs (id, organization_id, match_id, pause_type, "
                    "team_name, duration_minutes, reason, actor_id, created_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    pid, org_id, match_id, pause_type, team_name, duration_minutes,
                    reason, actor_id, utcnow(),
                )
                await org_service._audit(conn, org_id, actor_id, "pause.add",
                                         "match", match_id,
                                         {"minutes": duration_minutes, "reason": reason})
        return pid

    async def list_pauses(self, match_id: str) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_pause_logs WHERE match_id=$1 ORDER BY created_at", match_id)
            return [dict(r) for r in rows]

    # ---------- warnings ----------

    async def add_warning(self, org_id: str, match_id: str, player: str, rule: str,
                          *, team_name: str | None = None, note: str | None = None,
                          actor_id: str | None = None) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                wid = new_id()
                await conn.execute(
                    "INSERT INTO arb_warnings (id, organization_id, match_id, team_name, "
                    "player, rule, note, actor_id, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    wid, org_id, match_id, team_name, player, rule, note, actor_id, utcnow(),
                )
                await org_service._audit(conn, org_id, actor_id, "warning.add",
                                         "match", match_id,
                                         {"player": player, "rule": rule})
        return wid

    async def list_warnings(self, match_id: str) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_warnings WHERE match_id=$1 ORDER BY created_at", match_id)
            return [dict(r) for r in rows]

    # ---------- evidence ----------

    async def add_evidence(self, org_id: str, match_id: str, url: str,
                           *, note: str | None = None, submitted_by_id: str | None = None) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                eid = new_id()
                await conn.execute(
                    "INSERT INTO arb_evidence (id, organization_id, match_id, url, note, "
                    "status, submitted_by_id, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                    eid, org_id, match_id, url, note, EvidenceStatus.SUBMITTED.value,
                    submitted_by_id, utcnow(),
                )
                await org_service._audit(conn, org_id, submitted_by_id, "evidence.add",
                                         "match", match_id, {"url": url})
        return eid

    async def list_evidence(self, match_id: str, *, status: str | None = None) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM arb_evidence WHERE match_id=$1 AND status=$2 ORDER BY created_at",
                    match_id, status)
            else:
                rows = await conn.fetch(
                    "SELECT * FROM arb_evidence WHERE match_id=$1 ORDER BY created_at", match_id)
            return [dict(r) for r in rows]

    async def review_evidence(self, evidence_id: str, status: str,
                              reviewer_id: str, note: str | None = None) -> bool:
        if status not in (EvidenceStatus.REVIEWED.value, EvidenceStatus.REJECTED.value):
            raise OpsError("status: reviewed | rejected")
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE arb_evidence SET status=$1, reviewed_by_id=$2, reviewed_at=$3, "
                "review_note=$4 WHERE id=$5 RETURNING organization_id, match_id",
                status, reviewer_id, utcnow(), note, evidence_id)
            if row:
                await org_service._audit(conn, row["organization_id"], reviewer_id,
                                         "evidence.review", "evidence", evidence_id,
                                         {"status": status})
                return True
            return False

    # ---------- score reports ----------

    async def submit_score_report(
        self, org_id: str, match_id: str, team_a_score: int, team_b_score: int,
        *, team_a_result: str | None = None, team_b_result: str | None = None,
        scoring_type: str = "match", comment: str | None = None,
        attachments: list | None = None, submitted_by_id: str | None = None,
    ) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rid = new_id()
                await conn.execute(
                    "INSERT INTO arb_score_reports (id, organization_id, match_id, "
                    "team_a_score, team_b_score, team_a_result, team_b_result, scoring_type, "
                    "comment, attachments, status, submitted_by_id, created_at, updated_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                    rid, org_id, match_id, team_a_score, team_b_score, team_a_result,
                    team_b_result, scoring_type, comment, dumps(attachments or []),
                    ScoreReportStatus.PENDING.value, submitted_by_id, utcnow(), utcnow(),
                )
                await org_service._audit(conn, org_id, submitted_by_id,
                                         "score_report.submit", "match", match_id,
                                         {"a": team_a_score, "b": team_b_score})
        return rid

    async def list_score_reports(self, match_id: str | None = None,
                                 status: str | None = None) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            q = "SELECT * FROM arb_score_reports WHERE 1=1"
            args: list = []
            if match_id:
                args.append(match_id); q += f" AND match_id=${len(args)}"
            if status:
                args.append(status); q += f" AND status=${len(args)}"
            q += " ORDER BY created_at DESC LIMIT 50"
            rows = await conn.fetch(q, *args)
            return [dict(r) for r in rows]

    async def review_score_report(self, report_id: str, status: str,
                                  reviewer_id: str, note: str | None = None) -> dict | None:
        if status not in (ScoreReportStatus.APPROVED.value, ScoreReportStatus.REJECTED.value):
            raise OpsError("status: approved | rejected")
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE arb_score_reports SET status=$1, reviewed_by_id=$2, reviewed_at=$3, "
                "review_note=$4, updated_at=$5 WHERE id=$6 RETURNING *",
                status, reviewer_id, utcnow(), note, utcnow(), report_id)
            if row:
                await org_service._audit(conn, row["organization_id"], reviewer_id,
                                         "score_report.review", "score_report", report_id,
                                         {"status": status})
                return dict(row)
            return None

    # ---------- roster ----------

    async def submit_roster(self, org_id: str, match_id: str, team_slot: str,
                            team_name: str, players: list[dict],
                            *, note: str | None = None,
                            submitted_by_id: str | None = None) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rid = new_id()
                await conn.execute(
                    "INSERT INTO arb_roster_submissions (id, organization_id, match_id, "
                    "team_slot, team_name, players, status, note, submitted_by_id, "
                    "created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
                    "ON CONFLICT (match_id, team_slot) DO UPDATE SET players=$6, note=$8, "
                    "submitted_by_id=$9, status=$7, updated_at=$11",
                    rid, org_id, match_id, team_slot, team_name, dumps(players),
                    RosterStatus.SUBMITTED.value, note, submitted_by_id, utcnow(), utcnow(),
                )
                await org_service._audit(conn, org_id, submitted_by_id,
                                         "roster.submit", "match", match_id,
                                         {"slot": team_slot, "team": team_name})
        return rid

    async def list_rosters(self, match_id: str) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_roster_submissions WHERE match_id=$1", match_id)
            return [dict(r) for r in rows]

    async def review_roster(self, roster_id: str, status: str,
                            reviewer_id: str, note: str | None = None) -> bool:
        if status not in (RosterStatus.APPROVED.value, RosterStatus.REJECTED.value):
            raise OpsError("status: approved | rejected")
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE arb_roster_submissions SET status=$1, reviewed_by_id=$2, "
                "reviewed_at=$3, note=COALESCE($4, note), updated_at=$5 WHERE id=$6 "
                "RETURNING organization_id",
                status, reviewer_id, utcnow(), note, utcnow(), roster_id)
            if row:
                await org_service._audit(conn, row["organization_id"], reviewer_id,
                                         "roster.review", "roster", roster_id,
                                         {"status": status})
                return True
            return False

    # ---------- checkin ----------

    async def checkin(self, org_id: str, match_id: str, discord_user_id: str | int,
                      *, game_account: str | None = None,
                      validation: dict | None = None) -> str:
        profile = await org_service.get_or_create_profile_by_id(discord_user_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                cid = new_id()
                await conn.execute(
                    "INSERT INTO arb_checkins (id, organization_id, match_id, "
                    "user_profile_id, game_account, validation, created_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                    cid, org_id, match_id, profile.id, game_account,
                    dumps(validation) if validation else None, utcnow(),
                )
                await org_service._audit(conn, org_id, profile.id, "checkin",
                                         "match", match_id, {})
        return cid

    async def list_checkins(self, match_id: str) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_checkins WHERE match_id=$1 ORDER BY created_at", match_id)
            return [dict(r) for r in rows]

    # ---------- rulebook ----------

    async def upsert_rule(self, org_id: str, key: str, title: str, body: str,
                          *, category: str | None = None,
                          actor_id: str | None = None) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rid = new_id()
                await conn.execute(
                    "INSERT INTO arb_rulebook_entries (id, organization_id, key, title, body, "
                    "category, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
                    "ON CONFLICT (organization_id, key) DO UPDATE SET title=$4, body=$5, "
                    "category=$6, updated_at=$8",
                    rid, org_id, key, title, body, category, utcnow(), utcnow(),
                )
                await org_service._audit(conn, org_id, actor_id, "rule.upsert",
                                         "rulebook", key, {})
        return rid

    async def list_rules(self, org_id: str) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_rulebook_entries WHERE organization_id=$1 "
                "ORDER BY category NULLS LAST, key", org_id)
            return [dict(r) for r in rows]

    async def get_rule(self, org_id: str, key: str) -> dict | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM arb_rulebook_entries WHERE organization_id=$1 AND key=$2",
                org_id, key)
            return dict(row) if row else None

    # ---------- referee shifts ----------

    async def set_shift(self, org_id: str, discord_user_id: str | int,
                        on_shift: bool) -> None:
        profile = await org_service.get_or_create_profile_by_id(discord_user_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO arb_referee_shifts (id, organization_id, user_profile_id, "
                "on_shift, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6) "
                "ON CONFLICT (organization_id, user_profile_id) DO UPDATE SET "
                "on_shift=$4, updated_at=$6",
                new_id(), org_id, profile.id, on_shift, utcnow(), utcnow(),
            )

    async def list_on_shift(self, org_id: str) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT s.*, p.discord_user_id FROM arb_referee_shifts s "
                "JOIN arb_user_profiles p ON p.id = s.user_profile_id "
                "WHERE s.organization_id=$1 AND s.on_shift=TRUE", org_id)
            return [dict(r) for r in rows]

    # ---------- announcements ----------

    async def create_announcement(self, org_id: str, title: str, body: str,
                                  *, target_kind: str = "everyone",
                                  target_value: str | None = None,
                                  expires_at=None, actor_id: str | None = None) -> str:
        pool = await get_pool()
        async with pool.acquire() as conn:
            aid = new_id()
            await conn.execute(
                "INSERT INTO arb_announcements (id, organization_id, title, body, "
                "target_kind, target_value, created_by_id, expires_at, created_at, updated_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                aid, org_id, title, body, target_kind, target_value,
                actor_id, expires_at, utcnow(), utcnow(),
            )
            await org_service._audit(conn, org_id, actor_id, "announcement.create",
                                     "announcement", aid, {"title": title})
        return aid

    # ---------- timeline (audit read) ----------

    async def match_timeline(self, match_id: str, limit: int = 50) -> list[dict]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT a.*, p.discord_user_id AS actor_discord_id "
                "FROM arb_audit_log a LEFT JOIN arb_user_profiles p ON p.id = a.actor_id "
                "WHERE a.target_id=$1 ORDER BY a.created_at DESC LIMIT $2",
                match_id, limit)
            return [dict(r) for r in rows]


ops_service = OpsService()
