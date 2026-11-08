"""Разовый сброс пользовательского прогресса (PostgreSQL).

Используется и tools/reset_user_progress.py, и /admin resetprogress.
Все значения по умолчанию взяты из кода (models/player_stats.py,
storage/db.py, storage/user_balance_store.py, ultimatum/__init__.py):
ELO=1000, level=1, xp=0, xp_next=250, balance=100, bank=0.

Сохраняются: user_id/guild_id, имена, birthday, spouse_id,
description/bio/avatar_url; marriages, welcome_*, birthday_greetings,
teams, pets, каталоги, bot_settings, match numbering, banner settings.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

PLAYER_STATS_RESET = {
    "elo": 1000,
    "wins": 0,
    "finals": 0,
    "games": 0,
    "current_streak": 0,
    "best_win_streak": 0,
    "best_loss_streak": 0,
    "total_kills": 0,
    "total_deaths": 0,
    "total_assists": 0,
    "total_damage": 0,
    "best_match_kills": 0,
    "total_elo_change": 0,
    "last_elo_change": 0,
    "last_pet_elo_bonus": 0,
    "streak_bonus_coins": 0,
    "last_20_kills": 0,
    "last_20_deaths": 0,
    "last_20_wins": 0,
    "last_20_games": 0,
    "xp": 0,
    "level": 1,
    "xp_to_next_level": 250,
    "total_earnings": 0,
    "tournament_participations": 0,
    "reputation": 0,
    "daily_tournament_coins": 0,
    "daily_coins_date": "",
}

DELETE_TABLES = [
    "match_history",
    "elo_history",
    "question_history",
    "case_history",
    "user_inventory",
    "bets",
    "betting_stats",
    "bonus_cooldowns",
    "whoami_cooldowns",
    "minigame_stats",
    "minigame_sessions",
    "reputation_transactions",
    "rep_log_outbox",
    "rep_invites",
    "rep_boosts",
    "rep_giver_state",
    "banner_voice_daily",
    "banner_day_stats",
    "ultimatum_match_history",
    "ultimatum_audit_log",
    "ultimatum_votes",
    "ultimatum_rankings",
    "ultimatum_matches",
    "ultimatum_rounds",
    "ultimatum_players",
    "ultimatum_tournaments",
]

UPDATE_TABLES = {
    "user_balance": {"balance": 100},
    "user_bank": {"bank_balance": 0},
    "elo_ratings": {
        "elo": 1000, "peak_elo": 1000,
        "rated_matches": 0, "wins": 0, "losses": 0,
    },
}


async def _existing_tables(conn) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
    )
    return {r["table_name"] for r in rows}


async def _columns(conn, table: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = $1", table)
    return {r["column_name"] for r in rows}


async def collect_plan(conn) -> list[dict]:
    """План операций с фактическими объёмами строк."""
    tables = await _existing_tables(conn)
    plan = []

    if "player_stats" in tables:
        cols = await _columns(conn, "player_stats")
        applicable = {k: v for k, v in PLAYER_STATS_RESET.items() if k in cols}
        plan.append({
            "action": "UPDATE", "table": "player_stats",
            "rows": await conn.fetchval("SELECT COUNT(*) FROM player_stats"),
            "detail": f"{len(applicable)} progress columns -> defaults",
            "skipped_cols": sorted(set(PLAYER_STATS_RESET) - set(applicable)),
            "sets": applicable,
        })

    for table, sets in UPDATE_TABLES.items():
        if table not in tables:
            continue
        tcols = await _columns(conn, table)
        applicable = {k: v for k, v in sets.items() if k in tcols}
        plan.append({
            "action": "UPDATE", "table": table,
            "rows": await conn.fetchval(f"SELECT COUNT(*) FROM {table}"),
            "detail": f"{applicable} -> defaults",
            "skipped_cols": sorted(set(sets) - set(applicable)),
            "sets": applicable,
        })

    for table in DELETE_TABLES:
        if table in tables:
            plan.append({
                "action": "DELETE", "table": table,
                "rows": await conn.fetchval(f"SELECT COUNT(*) FROM {table}"),
                "detail": "all rows", "skipped_cols": [], "sets": {},
            })
    return plan


async def guard_problems(conn, tables: set[str]) -> list[str]:
    """Активные сущности, которые делают сброс небезопасным."""
    problems = []
    if "ultimatum_tournaments" in tables:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM ultimatum_tournaments "
            "WHERE status NOT IN ('COMPLETED','CANCELLED','FINISHED','DONE')"
        )
        if n:
            problems.append(
                f"активных ultimatum-турниров: {n}")
    if "mm_queues" in tables:
        n = await conn.fetchval("SELECT COUNT(*) FROM mm_queues")
        if n:
            problems.append(f"активных очередей Ranked: {n}")
    if "ready_checks" in tables:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM ready_checks WHERE status = 'READY_CHECK'")
        if n:
            problems.append(f"активных ready-check: {n}")
    return problems


async def execute_plan(conn, plan: list[dict]) -> list[str]:
    """Выполнить план внутри уже открытой транзакции. Вернуть лог."""
    results = []
    for step in plan:
        table = step["table"]
        if step["action"] == "DELETE":
            res = await conn.execute(f"DELETE FROM {table}")
        else:
            sets = step["sets"]
            if not sets:
                results.append(f"SKIP {table} (нет применимых колонок)")
                continue
            assignments = ", ".join(
                f"{k} = ${i + 1}" for i, k in enumerate(sets))
            res = await conn.execute(
                f"UPDATE {table} SET {assignments}", *sets.values())
        results.append(f"{step['action']} {table} -> {res}")
    return results


def format_plan(plan: list[dict], problems: list[str]) -> str:
    """Человекочитаемый отчёт для dry-run/подтверждения."""
    lines = ["**План сброса прогресса:**", "```"]
    for s in plan:
        lines.append(
            f"{s['action']:<6} {s['table']:<26} rows={s['rows']}")
    lines.append("```")
    if problems:
        lines.append("**ВНИМАНИЕ — активные сущности:**")
        lines.extend(f"- {p}" for p in problems)
    else:
        lines.append("Активных турниров/очередей не найдено.")
    return "\n".join(lines)


async def export_backup(conn, tables: list[str], path: Path) -> None:
    backup = {"created_at": datetime.now(timezone.utc).isoformat(),
              "tables": {}}
    for table in tables:
        rows = await conn.fetch(f"SELECT * FROM {table}")
        backup["tables"][table] = [
            {k: (v.isoformat() if isinstance(v, (datetime, date)) else v)
             for k, v in dict(r).items()}
            for r in rows
        ]
    path.write_text(
        json.dumps(backup, ensure_ascii=False, default=str),
        encoding="utf-8")
