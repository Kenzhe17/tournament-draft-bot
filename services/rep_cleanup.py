"""Очистка REP, начисленной за игровые действия (миграция, разовая).

Историческая игровая репутация начислялась в
utils/rating_calculator.py::update_player_stats_from_match:
+1 за участие в матче и +2 за победу команды — БЕЗ записи в
reputation_transactions. Поэтому точную сумму по журналу
восстановить нельзя: используется match_history как подтверждение
матчей, а «хвост» без подтверждения уходит в disputed-список и
НЕ списывается автоматически.

Алгоритм для каждого игрока:
    remainder  = reputation - journaled_rep - legacy_manual_rep
    estimate   = COUNT(match_history) + 2 * COUNT(win)
    deduct     = min(estimate, remainder)
    disputed   = remainder - deduct   (>= 0, не списывается)

journaled_rep — SUM(reputation_transactions.amount): все источники
журнала разрешены (/rep, приглашения, буст, тег, админ-корректировки).
legacy_manual_rep — выданные до Reputation 2.0 /rep из rep_given.json
(каждая запись targets = +1 REP). Если файл отсутствует — legacy
считать недоказанным, положительный remainder уходит в disputed.

Идемпотентность: запуск фиксируется в rep_cleanup_runs по op_id
(UNIQUE). Повторный запуск с тем же op_id — no-op. Все изменения
выполняются в одной транзакции вызывающим кодом.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config import DATA_DIR

logger = logging.getLogger(__name__)

# Уникальный идентификатор операции очистки — повторный запуск
# с тем же op_id ничего не списывает.
CLEANUP_OP_ID = "rep_game_cleanup_v1"

GAME_REP_PER_MATCH = 1   # +1 за участие
GAME_REP_WIN_BONUS = 2   # +2 за победу команды

_DISPUTED_THRESHOLD = 0  # disputed_amount > 0 -> игрок в списке на проверку


# ---------------------------------------------------------------------------
# Чтение входных данных
# ---------------------------------------------------------------------------

def load_legacy_manual_rep(path: Path | None = None) -> dict[tuple[int, int], int] | None:
    """REP, выданная /rep до Reputation 2.0 (rep_given.json).

    Формат: {"guild:giver": {"targets": ["uid", ...], ...}} —
    каждая цель = +1 REP получателю. Возвращает {(guild, user): rep}
    или None, если файл отсутствует/не читается (legacy недоказан).
    """
    path = path or (DATA_DIR / "rep_given.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("rep_given.json unreadable: %s", exc)
        return None
    if not isinstance(raw, dict):
        return None

    out: dict[tuple[int, int], int] = {}
    for key, entry in raw.items():
        try:
            guild_part = str(key).split(":", 1)[0]
            guild_id = int(guild_part)
        except (ValueError, IndexError):
            continue
        targets = entry.get("targets") if isinstance(entry, dict) else entry
        if not isinstance(targets, list):
            continue
        for t in targets:
            try:
                uid = int(t)
            except (TypeError, ValueError):
                continue
            if uid > 0:
                out[(guild_id, uid)] = out.get((guild_id, uid), 0) + 1
    return out


async def fetch_cleanup_inputs(
    conn, for_update: bool = False
) -> dict[str, Any]:
    """Собрать все входные данные для плана очистки.

    for_update=True — SELECT ... FOR UPDATE по player_stats: строка
    блокируется до конца транзакции, поэтому параллельные начисления
    REP 2.0 (/rep, invite, boost, tag, admin) либо завершатся до
    блокировки и попадут в план, либо подождут коммита cleanup и
    начислятся поверх уже очищенного баланса — потерь нет.

    Возвращает:
      stats_rows: [(guild_id, user_id, name, reputation)]
      tx_sums:    {(guild_id, user_id): int} — журнал REP 2.0
      game_est:   {(guild_id, user_id): int} — подтверждённая игровая REP
      match_history_available: bool
    """
    lock = " FOR UPDATE" if for_update else ""
    stats_rows = await conn.fetch(
        "SELECT guild_id, user_id, name, reputation"
        " FROM player_stats WHERE user_id > 0" + lock
    )

    tx_rows = await conn.fetch(
        "SELECT guild_id, user_id, COALESCE(SUM(amount), 0) AS total"
        " FROM reputation_transactions GROUP BY guild_id, user_id"
    )
    tx_sums = {
        (int(r["guild_id"]), int(r["user_id"])): int(r["total"])
        for r in tx_rows
    }

    game_est: dict[tuple[int, int], int] = {}
    match_history_available = True
    try:
        mh_rows = await conn.fetch(
            """
            SELECT guild_id, user_id,
                   COUNT(*) AS matches,
                   COUNT(*) FILTER (WHERE lower(btrim(result)) = 'win') AS wins
            FROM match_history
            GROUP BY guild_id, user_id
            """
        )
        for r in mh_rows:
            matches = int(r["matches"])
            wins = int(r["wins"])
            game_est[(int(r["guild_id"]), int(r["user_id"]))] = (
                matches * GAME_REP_PER_MATCH + wins * GAME_REP_WIN_BONUS
            )
    except Exception as exc:
        # Таблица может отсутствовать на старых БД — тогда вся
        # неучтённая REP уходит в disputed, авто-списания нет.
        match_history_available = False
        logger.warning("match_history unavailable, game REP unverifiable: %s", exc)

    return {
        "stats_rows": [
            (int(r["guild_id"]), int(r["user_id"]),
             r["name"] or "", int(r["reputation"] or 0))
            for r in stats_rows
        ],
        "tx_sums": tx_sums,
        "game_est": game_est,
        "match_history_available": match_history_available,
    }


# ---------------------------------------------------------------------------
# План очистки (pure — тестируется без БД)
# ---------------------------------------------------------------------------

def build_cleanup_plan(
    stats_rows: list[tuple[int, int, str, int]],
    tx_sums: dict[tuple[int, int], int],
    game_est: dict[tuple[int, int], int],
    legacy_manual: dict[tuple[int, int], int] | None,
    match_history_available: bool = True,
) -> list[dict[str, Any]]:
    """Построить план списания для всех игроков.

    Каждая запись: guild_id, user_id, name, rep_before,
    journaled_rep, legacy_manual_rep, remainder, game_rep_estimate,
    deduct, rep_after, disputed.
    """
    plan: list[dict[str, Any]] = []
    legacy_known = legacy_manual is not None
    legacy_manual = legacy_manual or {}

    for guild_id, user_id, name, rep_before in stats_rows:
        key = (guild_id, user_id)
        journaled = tx_sums.get(key, 0)
        legacy = legacy_manual.get(key, 0)
        allowed = journaled + legacy
        remainder = max(0, rep_before - allowed)

        estimate = game_est.get(key, 0) if match_history_available else 0

        if remainder <= 0:
            deduct = 0
            disputed = 0
        elif not legacy_known:
            # Не можем отличить игровую REP от legacy-/rep — не трогаем.
            deduct = 0
            disputed = remainder
        else:
            deduct = min(estimate, remainder)
            disputed = remainder - deduct

        plan.append({
            "guild_id": guild_id,
            "user_id": user_id,
            "name": name,
            "rep_before": rep_before,
            "journaled_rep": journaled,
            "legacy_manual_rep": legacy,
            "remainder": remainder,
            "game_rep_estimate": estimate,
            "deduct": deduct,
            "rep_after": rep_before - deduct,
            "disputed": disputed,
        })
    return plan


def disputed_players(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Игроки с REP, которую нельзя автоматически классифицировать."""
    return [p for p in plan if p["disputed"] > _DISPUTED_THRESHOLD]


# ---------------------------------------------------------------------------
# Применение (внутри транзакции вызывающего кода)
# ---------------------------------------------------------------------------

async def ensure_cleanup_tables(conn) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rep_cleanup_runs (
            op_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            report JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rep_cleanup_items (
            run_op_id TEXT NOT NULL REFERENCES rep_cleanup_runs(op_id),
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            rep_before INTEGER NOT NULL,
            journaled_rep INTEGER NOT NULL DEFAULT 0,
            legacy_manual_rep INTEGER NOT NULL DEFAULT 0,
            remainder INTEGER NOT NULL DEFAULT 0,
            game_rep_estimate INTEGER NOT NULL DEFAULT 0,
            deducted INTEGER NOT NULL DEFAULT 0,
            rep_after INTEGER NOT NULL,
            disputed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_op_id, guild_id, user_id)
        )
        """
    )


async def cleanup_already_ran(conn, op_id: str = CLEANUP_OP_ID) -> bool:
    row = await conn.fetchval(
        "SELECT 1 FROM rep_cleanup_runs WHERE op_id = $1", op_id
    )
    return row is not None


async def apply_cleanup_plan(
    conn,
    plan: list[dict[str, Any]],
    op_id: str = CLEANUP_OP_ID,
) -> dict[str, Any]:
    """Применить план в ТЕКУЩЕЙ транзакции conn.

    Возвращает сводку {applied, skipped_existing, total_deducted}.
    Повторный запуск (op_id уже есть) — no-op.
    """
    await ensure_cleanup_tables(conn)
    if await cleanup_already_ran(conn, op_id):
        return {"applied": False, "reason": "already_ran", "total_deducted": 0}

    total_deducted = 0
    applied = 0
    await conn.execute(
        "INSERT INTO rep_cleanup_runs (op_id, status) VALUES ($1, 'running')",
        op_id,
    )
    for p in plan:
        guild_id = p["guild_id"]
        user_id = p["user_id"]
        deduct = int(p["deduct"])
        rep_before = int(p["rep_before"])
        rep_after = rep_before - deduct
        if deduct > 0:
            # Списываем не больше текущего баланса (гонка с /rep).
            new_rep = await conn.fetchval(
                """
                UPDATE player_stats
                SET reputation = GREATEST(reputation - $3, 0)
                WHERE guild_id = $1 AND user_id = $2
                RETURNING reputation
                """,
                guild_id, user_id, deduct,
            )
            rep_after = int(new_rep) if new_rep is not None else rep_after
            total_deducted += deduct
            applied += 1
        await conn.execute(
            """
            INSERT INTO rep_cleanup_items
                (run_op_id, guild_id, user_id, name, rep_before,
                 journaled_rep, legacy_manual_rep, remainder,
                 game_rep_estimate, deducted, rep_after, disputed)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            op_id, guild_id, user_id, p.get("name", ""), rep_before,
            int(p.get("journaled_rep", 0)), int(p.get("legacy_manual_rep", 0)),
            int(p.get("remainder", 0)), int(p.get("game_rep_estimate", 0)),
            deduct, rep_after, int(p.get("disputed", 0)),
        )

    report = {
        "op_id": op_id,
        "players": len(plan),
        "players_deducted": applied,
        "total_deducted": total_deducted,
        "disputed_players": len(disputed_players(plan)),
        "disputed_total": sum(p["disputed"] for p in plan),
    }
    await conn.execute(
        """
        UPDATE rep_cleanup_runs
        SET status = 'done', report = $2::jsonb, finished_at = now()
        WHERE op_id = $1
        """,
        op_id, json.dumps(report, ensure_ascii=False),
    )
    return {"applied": True, "reason": "ok", **report}


# ---------------------------------------------------------------------------
# Бэкап и откат
# ---------------------------------------------------------------------------

async def export_backup(conn, path: Path) -> dict[str, int]:
    """Сохранить player_stats.reputation и reputation_transactions в JSON."""
    stats = await conn.fetch(
        "SELECT guild_id, user_id, name, reputation FROM player_stats"
    )
    txs = await conn.fetch(
        "SELECT id, guild_id, user_id, source, amount, actor_user_id,"
        " subject_user_id, event_key, created_at"
        " FROM reputation_transactions ORDER BY id"
    )
    payload = {
        "player_stats_reputation": [dict(r) for r in stats],
        "reputation_transactions": [
            {**dict(r), "created_at": r["created_at"].isoformat()}
            for r in txs
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return {
        "player_stats_rows": len(stats),
        "transactions_rows": len(txs),
    }


async def restore_cleanup(
    conn, op_id: str = CLEANUP_OP_ID
) -> dict[str, Any]:
    """Точечный откат очистки: вернуть ровно списанные суммы.

    В отличие от восстановления из JSON-бэкапа (который затёр бы
    REP, начисленную ПОСЛЕ миграции), здесь к текущему балансу
    прибавляется только deducted из rep_cleanup_items — новые
    легальные начисления (/rep, invite, boost, tag, admin)
    сохраняются.

    Идемпотентно: работает только для run со status='done',
    после отката status='restored' — повторный вызов no-op.
    Вызывающий код оборачивает в транзакцию.
    """
    row = await conn.fetchrow(
        "SELECT status FROM rep_cleanup_runs WHERE op_id = $1", op_id
    )
    if row is None:
        return {"restored": False, "reason": "no_run"}
    if row["status"] != "done":
        return {"restored": False, "reason": f"status={row['status']}"}

    items = await conn.fetch(
        """
        SELECT guild_id, user_id, deducted
        FROM rep_cleanup_items
        WHERE run_op_id = $1 AND deducted > 0
        """,
        op_id,
    )
    restored_rep = 0
    for it in items:
        res = await conn.execute(
            """
            UPDATE player_stats
            SET reputation = reputation + $3
            WHERE guild_id = $1 AND user_id = $2
            """,
            int(it["guild_id"]), int(it["user_id"]), int(it["deducted"]),
        )
        restored_rep += int(it["deducted"])

    await conn.execute(
        "UPDATE rep_cleanup_runs SET status = 'restored' WHERE op_id = $1",
        op_id,
    )
    return {
        "restored": True,
        "players": len(items),
        "restored_rep": restored_rep,
    }
