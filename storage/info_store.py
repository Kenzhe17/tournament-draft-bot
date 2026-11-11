"""Хранилище состояния информационного центра (bot_settings + JSON fallback).

Ключи в bot_settings (guild_id, key, value):
- info_channel_id — ID канала инфо-центра
- info_messages    — JSON {section_key: {message_id, version, updated_at}}
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from storage.db import get_pool, DATABASE_URL

logger = logging.getLogger(__name__)

from config import DATA_DIR

FALLBACK_PATH = DATA_DIR / "info_center.json"


class InfoStore:
    def __init__(self) -> None:
        self._mem: dict[int, dict] = {}  # guild_id -> {"channel_id": int, "messages": {}}
        self._loaded = False

    # ---------------- low-level settings ----------------

    async def _get_setting(self, guild_id: int, key: str) -> str | None:
        if DATABASE_URL:
            pool = await get_pool()
            async with pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT value FROM bot_settings WHERE guild_id=$1 AND key=$2",
                    guild_id, key,
                )
        return None

    async def _set_setting(self, guild_id: int, key: str, value: str) -> None:
        if DATABASE_URL:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO bot_settings (guild_id, key, value)
                    VALUES ($1,$2,$3)
                    ON CONFLICT (guild_id, key) DO UPDATE SET value=$3
                    """,
                    guild_id, key, value,
                )
            return
        self._mem.setdefault(guild_id, {})[key] = value
        self._save_fallback()

    def _save_fallback(self) -> None:
        try:
            FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
            FALLBACK_PATH.write_text(json.dumps(self._mem), encoding="utf-8")
        except Exception:
            logger.warning("info_center fallback save failed", exc_info=True)

    def _load_fallback(self) -> None:
        try:
            if FALLBACK_PATH.exists():
                self._mem = {
                    int(g): v for g, v in
                    json.loads(FALLBACK_PATH.read_text(encoding="utf-8")).items()
                }
        except Exception:
            logger.warning("info_center fallback load failed", exc_info=True)

    # ---------------- public API ----------------

    async def get_channel_id(self, guild_id: int) -> int | None:
        raw = await self._get_setting(guild_id, "info_channel_id")
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        if not DATABASE_URL:
            self._load_fallback()
            return self._mem.get(guild_id, {}).get("info_channel_id")
        return None

    async def set_channel_id(self, guild_id: int, channel_id: int) -> None:
        await self._set_setting(guild_id, "info_channel_id", str(channel_id))

    async def get_messages(self, guild_id: int) -> dict:
        raw = await self._get_setting(guild_id, "info_messages")
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                logger.warning("info_messages JSON corrupt for %s", guild_id)
                return {}
        if not DATABASE_URL:
            self._load_fallback()
            msgs = self._mem.get(guild_id, {}).get("info_messages")
            return msgs if isinstance(msgs, dict) else {}
        return {}

    async def set_message(
        self, guild_id: int, key: str, message_id: int, version: str
    ) -> None:
        msgs = await self.get_messages(guild_id)
        msgs[key] = {
            "message_id": message_id,
            "version": version,
            "updated_at": int(time.time()),
        }
        await self._set_setting(guild_id, "info_messages", json.dumps(msgs))


info_store = InfoStore()
