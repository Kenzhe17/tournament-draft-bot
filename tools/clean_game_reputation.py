"""Разовая очистка REP, начисленной за игровые действия (PostgreSQL).

Игровая REP (+1 за матч, +2 за победу) начислялась в
utils/rating_calculator.py без записи в reputation_transactions.
Скрипт списывает ТОЛЬКО подтверждённую по match_history сумму;
неучтённый остаток попадает в disputed-список и не трогается.

Использование:
    python tools/clean_game_reputation.py --dry-run
    python tools/clean_game_reputation.py --confirm --backup backup.json --report report.json
    python tools/clean_game_reputation.py --restore --confirm

--restore возвращает ровно списанные суммы из rep_cleanup_items
(не затирает REP, начисленную после миграции). JSON-бэкап из
--backup остаётся для аудита и ручного восстановления через pg_restore.

Требуется DATABASE_URL. Скрипт НЕ запускается автоматически и НЕ
выполняет списание без --confirm. Повторный запуск — no-op
(уникальный op_id rep_game_cleanup_v1 в rep_cleanup_runs).

Если rep_given.json отсутствует на сервере, без флага вся
неучтённая REP уходит в disputed и ничего не списывается.
--assume-no-legacy-manual-rep — явное подтверждение оператора,
что legacy /rep не использовался: тогда списывается
min(estimate match_history, remainder) на игрока.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg

from config import DATA_DIR, DATABASE_URL
from services import rep_cleanup

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("rep_cleanup")


async def run(args: argparse.Namespace) -> int:
    if not DATABASE_URL:
        log.error("DATABASE_URL is not set")
        return 2

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # --- Точечный откат списанной REP (не трогает REP,
        #     начисленную после миграции) ---
        if args.restore:
            if not args.confirm:
                log.error("--restore требует --confirm.")
                return 2
            await rep_cleanup.ensure_cleanup_tables(conn)
            async with conn.transaction():
                result = await rep_cleanup.restore_cleanup(conn)
            log.info("RESTORE result: %s", result)
            return 0 if result.get("restored") else 3

        # --- Сбор данных ---
        legacy = rep_cleanup.load_legacy_manual_rep()
        if legacy is None:
            if args.assume_no_legacy_manual_rep:
                # Оператор подтверждает: legacy /rep (rep_given.json)
                # на этом развёртывании не использовался/утерян,
                # считаем его вклад = 0. Остаток всё равно ограничен
                # match_history-оценкой; лишнее уходит в disputed.
                legacy = {}
                log.warning(
                    "--assume-no-legacy-manual-rep: rep_given.json "
                    "отсутствует, legacy /rep принят за 0."
                )
            else:
                log.warning(
                    "rep_given.json not found/unreadable — legacy /rep "
                    "считается недоказанным, положительная неучтённая REP "
                    "уйдёт в disputed без авто-списания."
                )
        inputs = await rep_cleanup.fetch_cleanup_inputs(conn)
        plan = rep_cleanup.build_cleanup_plan(
            inputs["stats_rows"],
            inputs["tx_sums"],
            inputs["game_est"],
            legacy,
            match_history_available=inputs["match_history_available"],
        )

        to_deduct = [p for p in plan if p["deduct"] > 0]
        disputed = rep_cleanup.disputed_players(plan)
        log.info(
            "Plan: %d players, %d with deduction, total -%d REP, "
            "%d disputed players (+%d REP unexplained)",
            len(plan), len(to_deduct),
            sum(p["deduct"] for p in plan),
            len(disputed), sum(p["disputed"] for p in plan),
        )
        for p in to_deduct[:50]:
            log.info(
                "  guild=%s user=%s '%s': rep %d -> %d (-%d, est=%d)",
                p["guild_id"], p["user_id"], p["name"],
                p["rep_before"], p["rep_after"], p["deduct"],
                p["game_rep_estimate"],
            )
        for p in disputed[:50]:
            log.warning(
                "  DISPUTED guild=%s user=%s '%s': +%d REP unexplained "
                "(rep=%d, journaled=%d, legacy=%d, est=%d)",
                p["guild_id"], p["user_id"], p["name"],
                p["disputed"], p["rep_before"], p["journaled_rep"],
                p["legacy_manual_rep"], p["game_rep_estimate"],
            )

        # Отчёт до/после по каждому игроку
        report_path = args.report or (DATA_DIR / "rep_cleanup_report.json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "op_id": rep_cleanup.CLEANUP_OP_ID,
                    "legacy_manual_known": legacy is not None,
                    "match_history_available": inputs["match_history_available"],
                    "players": plan,
                },
                ensure_ascii=False, indent=2, default=str,
            ),
            encoding="utf-8",
        )
        log.info("Report written: %s", report_path)

        if args.dry_run:
            log.info("DRY-RUN: ничего не изменено.")
            return 0
        if not args.confirm:
            log.error("Нужен --confirm (и рекомендуется --backup <file.json>).")
            return 2

        await rep_cleanup.ensure_cleanup_tables(conn)
        already = await rep_cleanup.cleanup_already_ran(conn)
        if already:
            log.info("Cleanup %s already ran — nothing to do.",
                     rep_cleanup.CLEANUP_OP_ID)
            return 0

        if args.backup:
            await rep_cleanup.export_backup(conn, args.backup)
            log.info("Backup written: %s", args.backup)
        else:
            log.warning("Бэкап не запрошен — рекомендую --backup или pg_dump.")

        # План + списание в ОДНОЙ транзакции с FOR UPDATE на
        # player_stats: параллельные /rep, invite, boost, tag и
        # admin-корректировки либо попадут в план (если закоммитятся
        # до блокировки), либо применятся поверх очищенного баланса
        # после коммита — начисления не теряются.
        async with conn.transaction():
            inputs = await rep_cleanup.fetch_cleanup_inputs(
                conn, for_update=True
            )
            plan = rep_cleanup.build_cleanup_plan(
                inputs["stats_rows"],
                inputs["tx_sums"],
                inputs["game_est"],
                legacy,
                match_history_available=inputs["match_history_available"],
            )
            # Отчёт по фактически применённому (заблокированному) плану
            report_path.write_text(
                json.dumps(
                    {
                        "op_id": rep_cleanup.CLEANUP_OP_ID,
                        "legacy_manual_known": legacy is not None,
                        "match_history_available": inputs["match_history_available"],
                        "players": plan,
                    },
                    ensure_ascii=False, indent=2, default=str,
                ),
                encoding="utf-8",
            )
            result = await rep_cleanup.apply_cleanup_plan(conn, plan)
        log.info("Cleanup result: %s", result)
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--backup", type=Path, default=None)
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--restore", action="store_true",
                    help="Откатить списание, вернув deducted из rep_cleanup_items")
    ap.add_argument("--assume-no-legacy-manual-rep", action="store_true",
                    help="rep_given.json отсутствует -> считать legacy /rep = 0 "
                         "(явное решение оператора; иначе вся неучтённая REP disputed)")
    args = ap.parse_args()
    args.dry_run = args.dry_run or not args.confirm
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
