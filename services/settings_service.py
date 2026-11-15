"""Runtime-сервис динамической конфигурации.

Единая точка чтения/записи настроек для бота и (в будущем) API панели.

Гарантии:
- PostgreSQL — единственный источник истины; значения кэшируются
  в памяти процесса и инвалидируются через LISTEN/NOTIFY
  (admin_config_changed) с периодическим poll-fallback;
- чтение никогда не ходит в БД на каждый get — кэш + lazy-load;
- set валидируется реестром, пишет values+history+audit атомарно,
  поддерживает optimistic concurrency (expected_version);
- при недоступности БД сервис работает в degraded-режиме: get
  возвращает default/legacy-значения, set отклоняется;
- применяется без рестарта: consumers читают через get/peek на
  каждую операцию (миграция consumers — отдельный этап).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from services import settings_registry as registry
from storage import admin_config_store as store

logger = logging.getLogger(__name__)

# ключ в реестре → legacy-ключ settings.json (переходный fallback,
# чтобы существующие /off-тоглы не слетели до миграции consumers).
LEGACY_SETTINGS_KEYS = {
    "feature.welcome_enabled": "welcome_enabled",
    "feature.coin_drop_enabled": "coin_drop_enabled",
}

_POLL_INTERVAL_SECONDS = 30.0


class SettingsUnavailableError(RuntimeError):
    """БД недоступна — запись невозможна."""


class _Missing:
    pass


_MISSING = _Missing()


class SettingsService:
    """Кэширующий фасад над admin_config_store."""

    def __init__(self, poll_interval: float = _POLL_INTERVAL_SECONDS) -> None:
        self._cache: dict[tuple[int, str], tuple[Any, int]] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._poll_interval = poll_interval
        self._listener_conn = None
        self._sync_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._stopped = False
        self.available = False

    # ---------------- lifecycle ----------------

    async def init(self) -> None:
        """Создать схему и загрузить все значения в кэш."""
        try:
            await store.init_schema()
            await self.refresh_all()
            self.available = True
        except Exception as exc:  # noqa: BLE001 — degraded-режим by design
            logger.error("settings service init failed: %s", exc)
            self.available = False

    async def start_sync(self) -> None:
        """Запустить LISTEN/NOTIFY-подписку и poll-fallback.

        Вызывать из bot loop после init(). Безопасно вызывать
        повторно — задачи не дублируются.
        """
        if self._sync_task is not None or self._poll_task is not None:
            return
        self._stopped = False
        self._sync_task = asyncio.ensure_future(self._listen_loop())
        self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def stop(self) -> None:
        self._stopped = True
        for task in (self._sync_task, self._poll_task):
            if task is not None:
                task.cancel()
        self._sync_task = self._poll_task = None
        # Соединение LISTEN освобождается контекстным менеджером
        # пула при завершении _listen_loop — здесь только ссылка.
        self._listener_conn = None

    # ---------------- чтение ----------------

    async def get(self, guild_id: int | None, key: str) -> Any:
        """Эффективное значение: guild override → global → default.

        Для scope=global ключа guild_id игнорируется (читается
        глобальная запись).
        """
        d = registry.get(key)
        if d is None:
            raise KeyError(f"unknown setting: {key}")
        effective_gid = self._resolve_gid(guild_id, d)

        value = await self._cached_value(effective_gid, key)
        if value is _MISSING:
            value = await self._legacy_fallback(key)
        if value is _MISSING:
            value = d.default
        try:
            return registry.coerce(d, value)
        except registry.SettingValidationError as exc:
            logger.warning("invalid stored value for %s: %s", key, exc)
            return d.default

    def peek(self, guild_id: int | None, key: str) -> Any:
        """Синхронное чтение из кэша (без обращения к БД).

        Для горячих путей, где async-вызов невозможен. Возвращает
        default, если ключ ещё не загружен — вызывающий код должен
        помнить, что значение может быть не свежим при первом
        обращении до init().
        """
        d = registry.get(key)
        if d is None:
            raise KeyError(f"unknown setting: {key}")
        effective_gid = self._resolve_gid(guild_id, d)
        entry = self._cache.get((effective_gid, key))
        raw = entry[0] if entry is not None else _MISSING
        if raw is _MISSING:
            raw = self._legacy_fallback_sync(key)
        if raw is _MISSING:
            raw = d.default
        try:
            return registry.coerce(d, raw)
        except registry.SettingValidationError:
            return d.default

    async def describe_setting(self, key: str) -> dict[str, Any]:
        """Описание + текущее значение + версия для API."""
        d = registry.get(key)
        if d is None:
            raise KeyError(f"unknown setting: {key}")
        value = await self.get(None, key)
        version = None
        if d.scope != "guild":
            entry = self._cache.get((0, key))
            if entry is not None:
                version = entry[1]
        return {
            "key": d.key,
            "type": d.type,
            "module": d.module,
            "description": d.description,
            "scope": d.scope,
            "default": d.default,
            "min": d.min,
            "max": d.max,
            "permission": d.permission,
            "runtime_editable": d.runtime_editable,
            "consumers_pending": d.consumers_pending,
            "applies_to": d.applies_to,
            "value": value,
            "version": version,
        }

    async def list_module(self, module: str) -> list[dict[str, Any]]:
        return [
            await self.describe_setting(d.key)
            for d in registry.all_defs()
            if d.module == module
        ]

    # ---------------- запись ----------------

    async def set(
        self,
        guild_id: int | None,
        key: str,
        value: Any,
        *,
        actor_id: int = 0,
        expected_version: int | None = None,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        d = registry.get(key)
        if d is None:
            raise KeyError(f"unknown setting: {key}")
        if not d.runtime_editable:
            raise registry.SettingValidationError(
                f"{key}: not runtime_editable"
            )
        effective_gid = self._resolve_gid(guild_id, d)
        coerced = registry.coerce(d, value)

        if not self.available:
            raise SettingsUnavailableError(
                "settings store unavailable (no database)"
            )

        row = await store.set_value(
            effective_gid, key, coerced,
            actor_id=actor_id,
            expected_version=expected_version,
            correlation_id=correlation_id,
        )
        self._cache[(effective_gid, key)] = (coerced, row["version"])
        return {
            "key": key,
            "guild_id": effective_gid,
            "value": coerced,
            "version": row["version"],
        }

    async def rollback(
        self,
        guild_id: int | None,
        key: str,
        target_version: int,
        *,
        actor_id: int = 0,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        d = registry.get(key)
        if d is None:
            raise KeyError(f"unknown setting: {key}")
        effective_gid = self._resolve_gid(guild_id, d)
        if not self.available:
            raise SettingsUnavailableError(
                "settings store unavailable (no database)"
            )
        row = await store.rollback_to_version(
            effective_gid, key, target_version,
            actor_id=actor_id, correlation_id=correlation_id,
        )
        # Значение из history уже провалидировано при записи;
        # на всякий случай приводим тип повторно.
        try:
            coerced = registry.coerce(d, row["value"])
        except registry.SettingValidationError:
            coerced = row["value"]
        self._cache[(effective_gid, key)] = (coerced, row["version"])
        return {
            "key": key,
            "guild_id": effective_gid,
            "value": coerced,
            "version": row["version"],
            "rolled_back_to": target_version,
        }

    async def history(
        self, guild_id: int | None, key: str, *,
        limit: int = 50, before_version: int | None = None,
    ) -> list[dict[str, Any]]:
        d = registry.get(key)
        if d is None:
            raise KeyError(f"unknown setting: {key}")
        effective_gid = self._resolve_gid(guild_id, d)
        return await store.get_history(
            effective_gid, key, limit=limit,
            before_version=before_version,
        )

    # ---------------- синхронизация ----------------

    async def refresh_all(self) -> int:
        """Полная перезагрузка кэша из БД. Возвращает число значений."""
        rows = await store.get_all_values()
        new_cache: dict[tuple[int, str], tuple[Any, int]] = {}
        for r in rows:
            key = r["key"]
            if registry.get(key) is None:
                continue  # ключи, удалённые из реестра, игнорируем
            new_cache[(int(r["guild_id"]), key)] = (
                r["value"], int(r["version"])
            )
        self._cache = new_cache
        return len(new_cache)

    async def refresh_key(self, guild_id: int, key: str) -> None:
        row = await store.get_value(guild_id, key)
        if row is None:
            self._cache.pop((guild_id, key), None)
        else:
            self._cache[(guild_id, key)] = (row["value"], row["version"])

    async def _listen_loop(self) -> None:
        """LISTEN admin_config_changed; при ошибке — backoff и retry."""
        from storage.db import get_pool

        backoff = 1.0
        while not self._stopped:
            try:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    self._listener_conn = conn
                    backoff = 1.0

                    def _on_notify(conn_, pid, channel, payload):
                        try:
                            data = json.loads(payload)
                        except (ValueError, TypeError):
                            return
                        gid = int(data.get("guild_id", 0))
                        key = data.get("key", "")
                        if key:
                            asyncio.ensure_future(
                                self._safe_refresh(gid, key)
                            )

                    await conn.add_listener(
                        store.NOTIFY_CHANNEL, _on_notify
                    )
                    logger.info(
                        "settings: LISTEN %s active", store.NOTIFY_CHANNEL
                    )
                    # Держим соединение, пока не отменят задачу.
                    while not self._stopped:
                        await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect с backoff
                logger.warning("settings listener error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                self._listener_conn = None

    async def _poll_loop(self) -> None:
        """Fallback-синхронизация: периодическая сверка версий."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._poll_diff()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — poll не должен падать
                logger.warning("settings poll error: %s", exc)

    async def _poll_diff(self) -> None:
        """Дешёвая сверка: читаем все версии, обновляем изменённые."""
        rows = await store.get_all_values()
        remote: dict[tuple[int, str], tuple[Any, int]] = {}
        for r in rows:
            if registry.get(r["key"]) is None:
                continue
            remote[(int(r["guild_id"]), r["key"])] = (
                r["value"], int(r["version"])
            )
        # Удалённые ключи: в PG строк не удаляем, поэтому
        # remote ⊇ кэш; обновляем только отличающиеся версии.
        for k, (value, version) in remote.items():
            cur = self._cache.get(k)
            if cur is None or cur[1] != version:
                self._cache[k] = (value, version)
        # Ключи, исчезнувшие из БД (теоретически), чистим —
        # но не negative-cache записи (_MISSING), чтобы не
        # превращать каждый get в запрос к БД.
        for k, entry in list(self._cache.items()):
            if k not in remote and entry[0] is not _MISSING:
                del self._cache[k]

    async def _safe_refresh(self, guild_id: int, key: str) -> None:
        try:
            await self.refresh_key(guild_id, key)
        except Exception as exc:  # noqa: BLE001 — refresh не должен падать
            logger.warning("settings refresh %s:%s failed: %s",
                           guild_id, key, exc)

    # ---------------- internals ----------------

    def _resolve_gid(self, guild_id: int | None, d) -> int:
        if d.scope == "global":
            return store.GLOBAL_SCOPE_GUILD_ID
        if d.scope == "guild":
            if not guild_id:
                raise ValueError(f"{d.key}: guild scope requires guild_id")
            return int(guild_id)
        # both: guild override if given, else global
        return int(guild_id) if guild_id else store.GLOBAL_SCOPE_GUILD_ID

    def _lock_for(self, gid: int, key: str) -> asyncio.Lock:
        k = (gid, key)
        lock = self._locks.get(k)
        if lock is None:
            lock = self._locks[k] = asyncio.Lock()
        return lock

    async def _cached_value(self, gid: int, key: str) -> Any:
        entry = self._cache.get((gid, key))
        if entry is not None:
            return entry[0]
        if not self.available:
            return _MISSING
        async with self._lock_for(gid, key):
            entry = self._cache.get((gid, key))
            if entry is not None:
                return entry[0]
            row = await store.get_value(gid, key)
            if row is None:
                self._cache[(gid, key)] = (_MISSING, 0)
                return _MISSING
            self._cache[(gid, key)] = (row["value"], row["version"])
            return row["value"]

    def _legacy_fallback_sync(self, key: str) -> Any:
        legacy_key = LEGACY_SETTINGS_KEYS.get(key)
        if not legacy_key:
            return _MISSING
        try:
            from storage.json_store import store as json_store
            settings = json_store.get_settings()
            if legacy_key in settings:
                return settings[legacy_key]
        except Exception as exc:  # noqa: BLE001 — legacy-fallback не критичен
            logger.debug("legacy settings fallback failed: %s", exc)
        return _MISSING

    async def _legacy_fallback(self, key: str) -> Any:
        return self._legacy_fallback_sync(key)


# ---------------- глобальный экземпляр ----------------

settings_service = SettingsService()
