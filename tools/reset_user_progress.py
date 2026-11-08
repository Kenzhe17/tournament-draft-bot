"""Разовый сброс пользовательского прогресса r!z3 (PostgreSQL).

Приводит игровые показатели пользователей к значениям по умолчанию,
которые использует код при первой регистрации. База, таблицы,
пользователи, настройки и социальные данные сохраняются.

Использование:
    python tools/reset_user_progress.py --dry-run      # только отчёт
    python tools/reset_user_progress.py --backup backup.json --confirm
    python tools/reset_user_progress.py --confirm --force   # игнорировать guard

Требуется DATABASE_URL (на Railway подставляется автоматически).
Скрипт НЕ запускается автоматически — только вручную.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg

from config import DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("reset_progress")

# ---------------------------------------------------------------------------
# Значения по умолчанию — из кода (models/player_stats.py, storage/db.py,
# storage/user_balance_store.py). ELO=1000, level=1, xp=0, xp_next=250,
# balance=100, bank=0.
# ---------------------------------------------------------------------------

# Колонки player_stats, которые являются прогрессом и обнуляются.
# Остальные (name, birthday, spouse_id, description, avatar_url, bio, ...)
# сохраняются — это социальные/профильные данные.
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

# Таблицы, очищаемые полностью (истории, статы, инвентарь, кулдауны).
# Пользовательские ID сохраняются там, где таблица не удаляется,
# а обновляется (user_balance, user_bank, elo_ratings).
DELETE_TABLES = [
    # Истории матчей и рейтингов
    "match_history",
    "elo_history",
    "question_history",
    "case_history",
    # Экономика: инвентарь, ставки, статистика ставок
    "user_inventory",
    "bets",
    "betting_stats",
    # Ежедневные награды и кулдауны
    "bonus_cooldowns",
    "whoami_cooldowns",
    # Мини-игры: статистика и сессии
    "minigame_stats",
    "minigame_sessions",
    # Репутация: журнал, outbox, ожидающие награды, кулдауны выдачи
    "reputation_transactions",
    "rep_log_outbox",
    "rep_invites",
    "rep_boosts",
    "rep_giver_state",
    # Баннер: голосовая активность и дневные снапшоты
    "banner_voice_daily",
    "banner_day_stats",
    # Ultimatum: история матчей и аудит (таблицы турниров — ниже,
    # с guard на активные турниры)
    "ultimatum_match_history",
    "ultimatum_audit_log",
    "ultimatum_votes",
    "ultimatum_rankings",
    "ultimatum_matches",
    "ultimatum_rounds",
    "ultimatum_players",
    "ultimatum_tournaments",
]

# Таблицы, где сбрасываются только числовые поля, строки сохраняются.
UPDATE_TABLES = {
    "user_balance": {"balance": 100},
    "user_bank": {"bank_balance": 0},
    "elo_ratings": {
        "elo": 1000, "peak_elo": 1000,
        "rated_matches": 0, "wins": 0, "losses": 0,
    },
}

# Таблицы/данные, которые НЕ трогаем (соцданные и конфигурация):
#   player_stats.name/birthday/spouse_id/description/avatar_url/bio
#   marriages, welcome_*, birthday_greetings, birthday (колонка)
#   teams, pets, pet_invitations — сущности, не статистика
#   shop_items, cases, questions, minigames — каталоги
#   bot_settings, banner_settings, banner_updates, match_counter,
#   match_numbers, ready_checks, mm_queues, db_migrations,
#   info_messages и прочие настройки каналов/ролей/эмодзи.


async def _existing_tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
    )
    return {r["table_name"] for r in rows}


async def _player_stats_columns(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'player_stats'"
    )
    return {r["column_name"] for r in rows}


async def _count(conn: asyncpg.Connection, table: str) -> int:
    return await conn.fetchval(f"SELECT COUNT(*) FROM {table}")


async def collect_report(conn: asyncpg.Connection) -> list[dict]:
    """Собрать план операций с фактическими объёмами данных."""
    tables = await _existing_tables(conn)
    plan = []

    # player_stats — UPDATE по реально существующим колонкам
    if "player_stats" in tables:
        cols = await _player_stats_columns(conn)
        applicable = {k: v for k, v in PLAYER_STATS_RESET.items() if k in cols}
        skipped = sorted(set(PLAYER_STATS_RESET) - set(applicable))
        n = await _count(conn, "player_stats")
        plan.append({
            "action": "UPDATE", "table": "player_stats",
            "rows": n, "detail": f"{len(applicable)} progress columns -> defaults",
            "skipped_cols": skipped,
            "sets": applicable,
        })

    for table, sets in UPDATE_TABLES.items():
        if table not in tables:
            continue
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1", table)
        tcols = {r["column_name"] for r in rows}
        applicable_t = {k: v for k, v in sets.items() if k in tcols}
        n = await _count(conn, table)
        plan.append({
            "action": "UPDATE", "table": table, "rows": n,
            "detail": f"{applicable_t} -> defaults",
            "skipped_cols": sorted(set(sets) - set(applicable_t)),
            "sets": applicable_t,
        })

    for table in DELETE_TABLES:
        if table not in tables:
            continue
        n = await _count(conn, table)
        plan.append({
            "action": "DELETE", "table": table, "rows": n,
            "detail": "all rows", "skipped_cols": [], "sets": {},
        })
    return plan


async def guard_active(conn: asyncpg.Connection, tables: set[str]) -> list[str]:
    """Проверки безопасности: активные турниры/очереди."""
    problems = []
    if "ultimatum_tournaments" in tables:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM ultimatum_tournaments "
            "WHERE status NOT IN ('COMPLETED','CANCELLED','FINISHED','DONE')"
        )
        if n:
            problems.append(
                f"ultimatum_tournaments: {n} активных турниров "
                "(статус не COMPLETED/CANCELLED)")
    if "mm_queues" in tables:
        n = await conn.fetchval("SELECT COUNT(*) FROM mm_queues")
        if n:
            problems.append(f"mm_queues: {n} активных очередей Ranked")
    if "ready_checks" in tables:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM ready_checks WHERE status = 'READY_CHECK'")
        if n:
            problems.append(f"ready_checks: {n} активных проверок готовности")
    return problems


async def export_backup(
    conn: asyncpg.Connection, tables: list[str], path: Path
) -> None:
    """Логический бэкап затрагиваемых таблиц в JSON."""
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
    log.info("Backup written: %s (%d tables)", path, len(tables))


async def execute_plan(
    conn: asyncpg.Connection, plan: list[dict]
) -> None:
    for step in plan:
        table = step["table"]
        if step["action"] == "DELETE":
            res = await conn.execute(f"DELETE FROM {table}")
        else:
            sets = step["sets"]
            if not sets:
                log.info("SKIP %s (no applicable columns)", table)
                continue
            assignments = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(sets))
            values = list(sets.values())
            res = await conn.execute(
                f"UPDATE {table} SET {assignments}", *values)
        log.info("%s %s -> %s", step["action"], table, res)


async def run(dry_run: bool, backup_path: Path | None,
              confirm: bool, force: bool) -> int:
    if not DATABASE_URL:
        log.error("DATABASE_URL is not set")
        return 2

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        tables = await _existing_tables(conn)
        plan = await collect_report(conn)

        log.info("=== RESET PLAN ===")
        total_rows = 0
        for step in plan:
            total_rows += step["rows"]
            log.info(
                "%-6s %-28s rows=%-8s %s%s",
                step["action"], step["table"], step["rows"],
                step["detail"],
                f"  skipped_cols={step['skipped_cols']}"
                if step["skipped_cols"] else "")
        log.info("Total affected rows (approx): %d", total_rows)

        problems = await guard_active(conn, tables)
        if problems:
            log.warning("=== GUARD ===")
            for p in problems:
                log.warning("  - %s", p)
            if not force:
                log.error("Активные сущности найдены. Остановите бота и "
                          "завершите турниры, либо повторите с --force.")
                return 3

        if dry_run:
            log.info("DRY-RUN: ничего не изменено.")
            return 0

        if not confirm:
            log.error("Для выполнения нужен --confirm "
                      "(и рекомендуется --backup <file.json>).")
            return 2

        # Бэкап затрагиваемых таблиц
        affected = [s["table"] for s in plan] + ["player_stats"]
        if backup_path:
            async with conn.transaction():
                await export_backup(conn, sorted(set(affected)), backup_path)
        else:
            log.warning("ВНИМАНИЕ: бэкап не запрошен (--backup). "
                        "Рекомендуется сделать pg_dump перед сбросом.")

        async with conn.transaction():
            await execute_plan(conn, plan)
        log.info("RESET COMPLETE — все операции в одной транзакции.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Только отчёт, без изменений (по умолчанию)")
    ap.add_argument("--confirm", action="store_true",
                    help="Выполнить сброс (обязательно для записи)")
    ap.add_argument("--backup", type=Path, default=None,
                    help="JSON-бэкап затрагиваемых таблиц перед сбросом")
    ap.add_argument("--force", action="store_true",
                    help="Игнорировать guard активных турниров/очередей")
    args = ap.parse_args()

    dry_run = args.dry_run or not args.confirm
    return asyncio.run(run(
        dry_run=dry_run, backup_path=args.backup,
        confirm=args.confirm, force=args.force))


if __name__ == "__main__":
    raise SystemExit(main())
