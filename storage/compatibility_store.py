"""Compatibility store to bridge old and new systems during migration.

Currently proxies all operations to the legacy JSON store so existing
code keeps working while the new services layer is being integrated.
"""

from __future__ import annotations

import logging
from typing import Optional

from models.tournament import Tournament
from storage.json_store import store as _json_store

logger = logging.getLogger(__name__)


class CompatibilityStore:
    """Store that proxies to the legacy JSON store.

    Sync methods delegate directly; async methods are provided for
    code that has been migrated to await-based calls.
    """

    def get_sync(self, guild_id: int, channel_id: int) -> Optional[Tournament]:
        return _json_store.get(guild_id, channel_id)

    async def get(self, guild_id: int, channel_id: int) -> Optional[Tournament]:
        return _json_store.get(guild_id, channel_id)

    def set_sync(self, tournament: Tournament) -> None:
        _json_store.set(tournament)

    async def set(self, tournament: Tournament) -> None:
        _json_store.set(tournament)

    def delete_sync(self, guild_id: int, channel_id: int) -> None:
        _json_store.delete(guild_id, channel_id)

    async def delete(self, guild_id: int, channel_id: int) -> None:
        _json_store.delete(guild_id, channel_id)

    def all_sync(self) -> list[Tournament]:
        return _json_store.all()

    async def all(self) -> list[Tournament]:
        return _json_store.all()

    def get_by_guild_sync(self, guild_id: int) -> list[Tournament]:
        return _json_store.get_by_guild(guild_id)

    async def get_by_guild(self, guild_id: int) -> list[Tournament]:
        return _json_store.get_by_guild(guild_id)


compatibility_store = CompatibilityStore()
