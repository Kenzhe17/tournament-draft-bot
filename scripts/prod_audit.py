"""Read-only pre-deploy audit against production PostgreSQL.
Run via: railway run --service core -- python scripts/prod_audit.py
Prints JSON report to stdout. Executes SELECTs only.
"""
import asyncio
import json
import os

import asyncpg


async def main():
    url = os.environ["DATABASE_URL"]
    # asyncpg не понимает railway proxy ?pgbouncer или sslmode-параметры
    url = url.replace("postgres://", "postgresql://")
    conn = await asyncpg.connect(url, ssl="prefer")
    out = {}

    async def q(name, sql):
        try:
            out[name] = [dict(r) for r in await conn.fetch(sql)]
        except Exception as exc:  # таблица может отсутствовать
            out[name] = {"error": str(exc)}

    async def scalar(name, sql):
        try:
            out[name] = await conn.fetchval(sql)
        except Exception as exc:
            out[name] = {"error": str(exc)}

    # --- Активные сессии ---
    await q("cashcup_active",
            "SELECT id, guild_id, status, created_at FROM cashcup_tournaments "
            "WHERE status IN ('registration','running') ORDER BY created_at DESC")
    await q("mm_queues", "SELECT guild_id, channel_id, queue_type, state, match_num FROM mm_queues")
    await q("ultimatum_active",
            "SELECT id, guild_id, status FROM ultimatum_tournaments "
            "WHERE status NOT IN ('COMPLETED','CANCELLED')")
    await q("ult2_active",
            "SELECT id, guild_id, status FROM ult2_tournaments "
            "WHERE status NOT IN ('COMPLETED','CANCELLED','FINISHED')")
    await q("spontaneous_active",
            "SELECT event_id, guild_id, game_type, state, deadline_at "
            "FROM spontaneous_events WHERE state = 'active'")
    await q("event_channel_state", "SELECT * FROM event_channel_state")
    await q("blackjack_open",
            "SELECT * FROM blackjack_rounds WHERE status = 'active' LIMIT 50")

    # --- Команды ---
    await q("teams_by_guild",
            "SELECT guild_id, COUNT(*) AS teams FROM teams GROUP BY guild_id ORDER BY teams DESC")
    await scalar("teams_total", "SELECT COUNT(*) FROM teams")
    await scalar("members_total",
                 "SELECT COALESCE(SUM(array_length(members,1)),0) FROM teams")
    await scalar("members_distinct",
                 "SELECT COUNT(DISTINCT uid) FROM teams, unnest(members) AS uid")

    # --- Legacy-конфликты ---
    await q("dup_names",
            "SELECT lower(team_name) AS n, COUNT(*) FROM teams GROUP BY 1 HAVING COUNT(*)>1")
    await q("multi_team_members",
            "SELECT guild_id, uid, COUNT(*) FROM teams, unnest(members) AS uid "
            "GROUP BY 1,2 HAVING COUNT(*)>1")
    await q("captain_not_in_roster",
            "SELECT team_id, team_name, captain_id, members FROM teams "
            "WHERE NOT (captain_id = ANY(members))")
    await q("oversized_rosters",
            "SELECT team_id, team_name, array_length(members,1) AS n FROM teams "
            "WHERE array_length(members,1) > 4")
    await q("password_teams",
            "SELECT team_id, team_name, captain_id, "
            "(password <> '') AS has_legacy_pw, (password_hash <> '') AS has_hash "
            "FROM teams WHERE password <> '' OR password_hash <> ''")
    await q("join_mode_dist",
            "SELECT join_mode, recruiting, COUNT(*) FROM teams GROUP BY 1,2")

    # --- Squad-таблицы (могут отсутствовать до миграции) ---
    for t in ("team_invites", "team_applications", "team_audit",
              "team_lfg", "squad_hub_state"):
        await scalar(f"count_{t}", f"SELECT COUNT(*) FROM {t}")

    # --- Прогресс/экономика ---
    for t in ("player_stats", "elo_ratings", "elo_history",
              "rep_balance", "reputation_transactions", "user_balance",
              "user_bank", "user_inventory", "shop_items",
              "match_history", "cashcup_audit", "tournament_journal",
              "tournament_audit_log"):
        await scalar(f"count_{t}", f"SELECT COUNT(*) FROM {t}")

    # --- Состояние БД ---
    await scalar("db_size", "SELECT pg_size_pretty(pg_database_size(current_database()))")
    await q("pg_version", "SELECT version()")
    await q("active_connections",
            "SELECT count(*) AS n, state FROM pg_stat_activity GROUP BY state")

    await conn.close()
    print(json.dumps(out, indent=2, default=str, ensure_ascii=False))


asyncio.run(main())
