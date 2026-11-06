"""Инициализация схемы Arbiter (arb_* таблицы) в PostgreSQL."""

from __future__ import annotations

import logging
from pathlib import Path

from storage.db import get_pool

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "storage" / "arbiter_schema.sql"


async def init_arbiter_schema() -> None:
    """Применить storage/arbiter_schema.sql (идемпотентно)."""
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    pool = await get_pool()
    async with pool.acquire() as conn:
        # asyncpg не выполняет несколько statement'ов с RETURNING в execute,
        # но наш файл — только DDL без параметров, поэтому execute ок.
        await conn.execute(sql)
    logger.info("Arbiter schema initialized (%d bytes)", len(sql))
