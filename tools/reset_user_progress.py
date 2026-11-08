"""Разовый сброс пользовательского прогресса r!z3 (PostgreSQL).

CLI-обёртка над services/progress_reset.py — та же логика, что и
/admin resetprogress. Используйте для запуска вне Discord.

Использование:
    python tools/reset_user_progress.py --dry-run          # только отчёт
    python tools/reset_user_progress.py --backup b.json --confirm
    python tools/reset_user_progress.py --confirm --force  # игнорировать guard

Требуется DATABASE_URL. Скрипт НЕ запускается автоматически.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg

from config import DATABASE_URL
from services.progress_reset import (
    collect_plan, execute_plan, export_backup, format_plan,
    guard_problems, _existing_tables,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("reset_progress")


async def run(dry_run: bool, backup_path: Path | None,
              confirm: bool, force: bool) -> int:
    if not DATABASE_URL:
        log.error("DATABASE_URL is not set")
        return 2

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        tables = await _existing_tables(conn)
        plan = await collect_plan(conn)
        problems = await guard_problems(conn, tables)

        log.info("=== RESET PLAN ===")
        for s in plan:
            log.info("%-6s %-28s rows=%-8s %s%s",
                     s["action"], s["table"], s["rows"], s["detail"],
                     f"  skipped={s['skipped_cols']}" if s["skipped_cols"] else "")
        log.info("Total rows (approx): %d",
                 sum(s["rows"] for s in plan))
        for p in problems:
            log.warning("GUARD: %s", p)

        if dry_run:
            log.info("DRY-RUN: ничего не изменено.")
            return 0
        if not confirm:
            log.error("Нужен --confirm (и рекомендуется --backup <file.json>).")
            return 2
        if problems and not force:
            log.error("Активные сущности найдены. Завершите их "
                      "или повторите с --force.")
            return 3

        if backup_path:
            async with conn.transaction():
                await export_backup(
                    conn, sorted({s["table"] for s in plan} | {"player_stats"}),
                    backup_path)
        else:
            log.warning("Бэкап не запрошен — рекомендую --backup или pg_dump.")

        async with conn.transaction():
            results = await execute_plan(conn, plan)
        for r in results:
            log.info("  %s", r)
        log.info("RESET COMPLETE (single transaction).")
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--backup", type=Path, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    dry_run = args.dry_run or not args.confirm
    return asyncio.run(run(dry_run, args.backup, args.confirm, args.force))


if __name__ == "__main__":
    raise SystemExit(main())
