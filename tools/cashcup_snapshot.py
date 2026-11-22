"""Одноразовый перенос снимка CASH CUP: production → staging.

Экспорт СТРОГО read-only: отдельное подключение к production PostgreSQL
с SET TRANSACTION READ ONLY, только SELECT. Никаких UPDATE/INSERT/DELETE
в production не выполняется и не может быть выполнено этим скриптом.

Использование:

  # 1) Экспорт активного Cash Cup из production в JSON (секреты не печатаются)
  set PROD_DATABASE_URL=postgres://...   # production DB (только чтение)
  python tools/cashcup_snapshot.py export --out data/cashcup_snapshot.json

  # 2) Импорт снимка в staging PostgreSQL как тестовую сессию
  set DATABASE_URL=postgres://...        # staging DB
  python tools/cashcup_snapshot.py import --file data/cashcup_snapshot.json

  # 3) Проверка без записи
  python tools/cashcup_snapshot.py check --file data/cashcup_snapshot.json

Скрипт НИКОГДА не использует production DATABASE_URL для записи:
import подключается только к DATABASE_URL окружения (staging).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

DEFAULT_PROD_GUILD_ID = 1196165488565289012  # прод-сервер r!z3 (только чтение)

# Ключи, которые не должны покидать production даже в экспортируемом
# снимке: пароли комнат, токены, webhook-секреты.
_SENSITIVE_KEYS = ("password", "secret", "token", "webhook")


def _sanitize(obj):
    """Рекурсивно удаляет чувствительные ключи из снимка."""
    if isinstance(obj, dict):
        return {
            k: _sanitize(v) for k, v in obj.items()
            if not any(s in str(k).lower() for s in _SENSITIVE_KEYS)
        }
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


async def export_snapshot(database_url: str, guild_id: int,
                          out_path: Path) -> dict:
    """Read-only выгрузка активного Cash Cup из production."""
    conn = await asyncpg.connect(database_url)
    try:
        async with conn.transaction(readonly=True):
            row = await conn.fetchrow("""
                SELECT id, guild_id, channel_id, status, creator_id,
                       state, version, created_at
                FROM cashcup_tournaments
                WHERE guild_id=$1 AND status IN ('registration','running')
                ORDER BY created_at DESC LIMIT 1
            """, guild_id)
            if row is None:
                row = await conn.fetchrow("""
                    SELECT id, guild_id, channel_id, status, creator_id,
                           state, version, created_at
                    FROM cashcup_tournaments
                    WHERE guild_id=$1
                    ORDER BY created_at DESC LIMIT 1
                """, guild_id)
        if row is None:
            raise SystemExit("В production не найдено ни одного CASH CUP")
        raw = row["state"]
        state = json.loads(raw) if isinstance(raw, str) else dict(raw)
        payload = {
            "exported_at": datetime.now(UTC).isoformat(),
            "source": "production-readonly",
            "guild_id": int(row["guild_id"]),
            "tournament_id": row["id"],
            "status": row["status"],
            "version": row["version"],
            "state": _sanitize(state),
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return payload
    finally:
        await conn.close()


def _team_counts(state: dict) -> dict:
    teams = state.get("teams") or []
    members = sum(len(t.get("members") or []) for t in teams)
    ff_ok = sum(1 for t in teams for p in (t.get("members") or [])
                if p.get("ff_ok") or p.get("ff_uid"))
    verified = sum(1 for t in teams for p in (t.get("members") or [])
                   if p.get("verified"))
    return {"teams": len(teams), "members": members,
            "ff_profiles": ff_ok, "verified": verified}


def _uid_collisions(state: dict) -> list[str]:
    seen: dict[str, str] = {}
    collisions = []
    for t in state.get("teams") or []:
        for p in (t.get("members") or []) + (t.get("pending") or []):
            uid = str(p.get("ff_uid") or "")
            if not uid:
                continue
            if uid in seen and seen[uid] != t.get("name"):
                collisions.append(uid)
            seen.setdefault(uid, t.get("name") or "?")
    return collisions


async def import_snapshot(database_url: str, guild_id: int, channel_id: int,
                          creator_id: int, snapshot_path: Path) -> dict:
    """Импорт снимка в staging БД как новую тестовую сессию."""
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    state = payload.get("state") or {}
    if not state.get("teams"):
        raise SystemExit("В снимке нет команд — импорт отменён")

    # Локальный импорт store без запуска бота.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import services.cashcup_test_store as cts
    import storage.db as dbmod

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = database_url
    dbmod._pool = pool
    cts.cashcup_test_store._ready = False
    try:
        session = await cts.cashcup_test_store.create_snapshot(
            guild_id, channel_id, creator_id, state)
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()
    return {"session_id": session["session_id"],
            "status": session["status"], **_team_counts(state),
            "uid_collisions": _uid_collisions(state)}


async def check_snapshot(snapshot_path: Path) -> dict:
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    state = payload.get("state") or {}
    counts = _team_counts(state)
    return {"teams": counts, "uid_collisions": _uid_collisions(state),
            "status": payload.get("status"),
            "tournament_id": payload.get("tournament_id")}


def main() -> None:
    ap = argparse.ArgumentParser(description="CASH CUP snapshot export/import")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_exp = sub.add_parser("export", help="read-only экспорт из production")
    p_exp.add_argument("--out", required=True)
    p_exp.add_argument("--guild", type=int, default=DEFAULT_PROD_GUILD_ID)
    p_imp = sub.add_parser("import", help="импорт снимка в staging БД")
    p_imp.add_argument("--file", required=True)
    p_imp.add_argument("--guild", type=int, required=True,
                       help="тестовый Discord guild staging")
    p_imp.add_argument("--channel", type=int, default=0)
    p_imp.add_argument("--creator", type=int, default=0)
    p_chk = sub.add_parser("check", help="проверка файла снимка")
    p_chk.add_argument("--file", required=True)
    args = ap.parse_args()

    if args.cmd == "export":
        url = os.getenv("PROD_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not url:
            raise SystemExit("Задайте PROD_DATABASE_URL (production, read-only)")
        payload = asyncio.run(export_snapshot(url, args.guild,
                                              Path(args.out)))
        print(json.dumps({"ok": True, "out": args.out,
                          "tournament_id": payload["tournament_id"],
                          "status": payload["status"],
                          **_team_counts(payload["state"]),
                          "uid_collisions": _uid_collisions(payload["state"])},
                         ensure_ascii=False, indent=2))
    elif args.cmd == "import":
        url = os.getenv("DATABASE_URL")
        if not url:
            raise SystemExit("Задайте DATABASE_URL (staging)")
        result = asyncio.run(import_snapshot(url, args.guild, args.channel,
                                             args.creator, Path(args.file)))
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(asyncio.run(check_snapshot(Path(args.file))),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
