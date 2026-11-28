"""Логический бэкап PostgreSQL в JSONL (read-only для БД).

Запуск на хосте с доступом к DATABASE_URL (локально через
``railway run`` или внутри контейнера):

    railway run python scripts/pg_snapshot.py --out ./pg_backup_$(date +%Y%m%d)

Для каждой таблицы public-схемы пишет ``<table>.jsonl`` (по строке
на запись) и ``MANIFEST.json`` с числом строк и SHA-256 каждого файла.
Ничего в БД не изменяет. Восстановление — точечное, по таблицам;
НЕ использовать для отката всей БД (потеряет новые транзакции).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path


async def dump(database_url: str, out_dir: Path) -> int:
    import asyncpg

    out_dir.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(database_url)
    try:
        tables = await conn.fetch(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        manifest = {"tables": {}, "database_url_host": "***"}
        total_rows = 0
        for (table,) in [(r["table_name"],) for r in tables]:
            # имя из information_schema — безопасно для %I
            rows = await conn.fetch(f'SELECT * FROM "{table}"')
            path = out_dir / f"{table}.jsonl"
            h = hashlib.sha256()
            with open(path, "w", encoding="utf-8") as f:
                for row in rows:
                    line = json.dumps(
                        dict(row), default=str, ensure_ascii=False
                    ) + "\n"
                    f.write(line)
                    h.update(line.encode("utf-8"))
            manifest["tables"][table] = {
                "rows": len(rows),
                "sha256": h.hexdigest(),
                "file": path.name,
            }
            total_rows += len(rows)
            print(f"  {table}: {len(rows)} rows")
        manifest["total_rows"] = total_rows
        (out_dir / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"ИТОГО: {len(tables)} таблиц, {total_rows} строк -> {out_dir}")
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL не задан", file=sys.stderr)
        return 2
    return asyncio.run(dump(url, args.out))


if __name__ == "__main__":
    sys.exit(main())
