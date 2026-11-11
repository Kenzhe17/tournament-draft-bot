"""JSON-хранилище состояния турниров.

Атомарная запись через storage.json_atomic: данные пишутся во временный
файл, fsync, os.replace — читатели никогда не видят частичный файл.
Предыдущая валидная версия хранится в <name>.bak (обновляется
атомарно и только из валидного JSON).

Защита от порчи: при битом основном файле оригинал откладывается в
*.corrupt-<ts>, затем пробуется .bak. Если ни одна копия не пригодна,
стор переходит в unhealthy: save() отказывается перезаписывать файл,
чтобы пустое состояние не затёрло повреждённые данные — требуется
ручное восстановление.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import DATA_DIR, DATA_FILE
from models.tournament import Tournament
from storage.json_atomic import (
    atomic_write_text,
    backup_path,
    load_json_file,
    rotate_backup,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class StoreUnavailableError(RuntimeError):
    """Хранилище не может надёжно сохранить данные.

    Выбрасывается из set/delete/save, когда файл повреждён
    (unhealthy) или запись на диск невозможна. Вызывающий код
    не должен подтверждать пользователю успешную операцию.
    """


def _corrupt_path(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.corrupt-{ts}")


# ---------------------------------------------------------------------------
# Sharded-режим (JSON_STORE_MODE=sharded)
#
# Каждый турнир — отдельный атомарный JSON-файл в
# DATA_DIR/tournaments/<guild_id>_<channel_id>.json (+ .bak).
# Манифест — DATA_DIR/tournaments.manifest.json (список ключей для
# обнаружения потерянных/лишних shard-файлов при загрузке).
#
# Миграция legacy→sharded: staging в tournaments.staging/ →
# проверка каждого файла → атомарный os.rename каталога →
# manifest. Legacy tournaments.json НЕ удаляется (rollback).
# Прерванная миграция безопасна: любой незавершённый каталог
# откладывается в *.corrupt-<ts>, миграция перезапускается
# с последней подтверждённой версии (legacy-файл).
# ---------------------------------------------------------------------------

SHARD_DIR_NAME = "tournaments"
STAGING_DIR_NAME = "tournaments.staging"
MANIFEST_NAME = "tournaments.manifest.json"


def _shard_filename(guild_id: int, channel_id: int) -> str:
    """Устойчивое имя shard-файла из внутренних ID (безопасные символы)."""
    return f"{int(guild_id)}_{int(channel_id)}.json"


def _parse_shard_name(path: Path) -> tuple[int, int] | None:
    """'123_456.json' -> (123, 456); None — не shard-файл."""
    stem = path.stem
    if path.suffix != ".json" or "_" not in stem:
        return None
    a, _, b = stem.partition("_")
    try:
        return int(a), int(b)
    except ValueError:
        return None


class JsonStore:
    """Сохранение и загрузка турниров.

    Режимы (env JSON_STORE_MODE):
    - "single"/"legacy" — один tournaments.json (историческое
      поведение, все гарантии corrupt-recovery).
    - "sharded" — по файлу на турнир; store.set() сериализует
      и пишет ТОЛЬКО изменённый турнир (~КБ вместо всего состояния).
    - не задан — auto: sharded, если уже есть shard-файлы/манифест;
      иначе single (legacy-файл остаётся источником до явного
      включения sharded — миграция только по запросу).
    """

    def __init__(self) -> None:
        self._tournaments: dict[tuple[int, int], Tournament] = {}
        self._save_lock = threading.Lock()
        self._unhealthy = False
        self._shard_dir = DATA_DIR / SHARD_DIR_NAME
        self._staging_dir = DATA_DIR / STAGING_DIR_NAME
        self._manifest_path = DATA_DIR / MANIFEST_NAME
        self._mode = self._resolve_mode()
        self._manifest_keys: set[str] = set()
        self._lost_keys: list[str] = []
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def _resolve_mode(self) -> str:
        """single | sharded по env/наличию shard-данных."""
        mode = os.getenv("JSON_STORE_MODE", "").strip().lower()
        if mode in ("single", "legacy"):
            return "single"
        if mode in ("sharded", "shard"):
            return "sharded"
        # auto: sharded только если shard-данные уже существуют —
        # иначе остаёмся на проверенном single-режиме.
        if self._manifest_path.exists():
            return "sharded"
        if self._shard_dir.is_dir() and any(
            self._shard_dir.glob("*.json")
        ):
            return "sharded"
        return "single"

    def is_healthy(self) -> bool:
        """False — хранилище повреждено и требует ручного восстановления."""
        return not self._unhealthy

    def corrupt_keys(self) -> list[str]:
        """Shard-ключи, не восстановленные при последней загрузке."""
        return list(self._lost_keys)

    def _shard_path(self, key: tuple[int, int]) -> Path:
        return self._shard_dir / _shard_filename(*key)

    def _archive_incomplete(self, path: Path) -> None:
        """Отложить незавершённый каталог/файл в *.corrupt-<ts>."""
        import shutil

        try:
            target = path.with_name(
                f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
            )
            if path.is_dir():
                shutil.move(str(path), str(target))
            elif path.exists():
                os.replace(path, target)
            else:
                return
            logger.warning(
                "Незавершённое состояние %s отложено в %s",
                path.name, target.name,
            )
        except OSError as exc:
            logger.error("Не удалось отложить %s: %s", path, exc)

    def _ensure_writable(self) -> None:
        """Проверить, что стор может сохранять.

        При unhealthy сначала пробует восстановиться (админ мог
        подменить файл валидной копией). Если восстановление не
        удалось — явная ошибка вместо молчаливого no-op.
        """
        if not self._unhealthy:
            return
        logger.warning(
            "JsonStore unhealthy — пробую восстановление из %s / %s",
            DATA_FILE, backup_path(DATA_FILE),
        )
        self.load()
        if self._unhealthy:
            raise StoreUnavailableError(
                f"Хранилище турниров повреждено: {DATA_FILE}. "
                "Сохранение невозможно до ручного восстановления "
                "из *.bak / *.corrupt-*."
            )
        logger.warning("JsonStore восстановлен из резервной копии")

    def load(self) -> None:
        """Загрузить все турниры.

        Режим sharded — отдельные файлы на турнир; single —
        историческая логика tournaments.json + .bak.
        """
        self._unhealthy = False
        self._lost_keys = []
        if self._mode == "sharded":
            self._load_sharded()
            return
        self._warn_if_shards_newer()
        if not DATA_FILE.exists():
            # Файла нет: сначала пробуем .bak (основной мог быть
            # отложен как *.corrupt-* при прошлой порче), затем
            # отличаем первый запуск от последствий повреждения.
            if self._restore_from_backup():
                return
            if list(DATA_DIR.glob(f"{DATA_FILE.name}.corrupt-*")):
                self._unhealthy = True
                logger.critical(
                    "КРИТИЧНО: %s отсутствует, а файлы *.corrupt-* "
                    "свидетельствуют о прошлой порче. Стор НЕ будет "
                    "создавать пустое состояние поверх — требуется "
                    "ручное восстановление из *.bak / *.corrupt-*.",
                    DATA_FILE,
                )
            self._tournaments = {}
            return

        raw = load_json_file(DATA_FILE)
        if raw is None:
            # Сохраняем повреждённый файл для диагностики
            try:
                os.replace(DATA_FILE, _corrupt_path(DATA_FILE))
            except OSError as exc:
                logger.error("Не удалось отложить битый файл: %s", exc)
            if self._restore_from_backup():
                return
            self._unhealthy = True
            logger.critical(
                "КРИТИЧНО: %s повреждён и нет валидной резервной "
                "копии. Стор работает в режиме «только чтение памяти» "
                "и НЕ будет перезаписывать файл. Требуется ручное "
                "восстановление из *.corrupt-* / *.bak.",
                DATA_FILE,
            )
            self._tournaments = {}
            return

        migrated = self._migrate(raw)
        if migrated is None:
            # Структура не читается — пробуем backup, как при битом JSON
            if self._restore_from_backup():
                return
            self._unhealthy = True
            logger.critical(
                "КРИТИЧНО: структура %s не читается и нет валидной "
                "резервной копии. Сохранения отключены до "
                "восстановления.",
                DATA_FILE,
            )
            self._tournaments = {}
            return

        self._tournaments = migrated
        logger.info("Загружено %d турниров", len(self._tournaments))

    def _warn_if_shards_newer(self) -> None:
        """Single-режим при наличии shard-файлов: legacy может быть
        устаревшим — громкое предупреждение, без молчаливой загрузки."""
        try:
            shards = [
                p for p in self._shard_dir.glob("*.json")
                if _parse_shard_name(p) is not None
            ]
        except OSError:
            return
        if not shards:
            return
        try:
            legacy_mtime = DATA_FILE.stat().st_mtime if DATA_FILE.exists() else 0
            newest = max(p.stat().st_mtime for p in shards)
        except OSError:
            return
        if newest > legacy_mtime:
            logger.critical(
                "ВНИМАНИЕ: обнаружены shard-файлы (%s) новее %s — "
                "single-режим загрузит УСТАРЕВШИЕ данные. "
                "Для безопасного rollback используйте "
                "storage.json_store.export_shards_to_legacy().",
                len(shards), DATA_FILE.name,
            )

    # ------------------------------------------------------------------
    # Sharded-режим
    # ------------------------------------------------------------------

    def _load_sharded(self) -> None:
        """Загрузка в sharded-режиме: миграция при необходимости +
        чтение shard-файлов с .bak-fallback и manifest-контролем."""
        manifest = self._read_manifest()
        if manifest is None:
            # Незавершённые состояния миграции — откладываем в сторону.
            if self._staging_dir.exists():
                self._archive_incomplete(self._staging_dir)
            has_orphan_shards = self._shard_dir.is_dir() and any(
                _parse_shard_name(p) is not None
                for p in self._shard_dir.glob("*.json")
            )
            if has_orphan_shards:
                # shard-файлы без манифеста — прерванный коммит
                # миграции; legacy — последняя подтверждённая версия.
                self._archive_incomplete(self._shard_dir)

            if DATA_FILE.exists():
                if self._migrate_legacy_to_shards():
                    manifest = self._read_manifest()
                else:
                    # Миграция не удалась — безопасный fallback на
                    # single-режим (legacy-файл не тронут).
                    logger.error(
                        "Миграция в sharded-режим не удалась — "
                        "переход на single-режим"
                    )
                    self._mode = "single"
                    self._load_single()
                    return
            # else: чистый старт — пустой shard-каталог.

        self._shard_dir.mkdir(parents=True, exist_ok=True)

        # Чтение shard-файлов
        loaded_keys: set[str] = set()
        for path in sorted(self._shard_dir.glob("*.json")):
            parsed = _parse_shard_name(path)
            if parsed is None:
                continue
            gid, cid = parsed
            key_name = f"{gid}_{cid}"
            raw = load_json_file(path)
            if raw is None:
                # Пробуем .bak этого shard'а
                raw = load_json_file(backup_path(path))
                if raw is not None:
                    logger.warning(
                        "Shard %s восстановлен из .bak", path.name
                    )
            if raw is None:
                self._lost_keys.append(key_name)
                logger.critical(
                    "КРИТИЧНО: shard %s повреждён и .bak недоступен — "
                    "турнир не загружен, требуется ручное восстановление",
                    path.name,
                )
                continue
            try:
                t = Tournament.from_dict(raw)
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                self._lost_keys.append(key_name)
                logger.critical(
                    "КРИТИЧНО: shard %s не парсится (%s) — турнир "
                    "не загружен", path.name, exc,
                )
                continue
            if (t.guild_id, t.channel_id) != (gid, cid):
                logger.error(
                    "Shard %s: внутренние ID (%s,%s) != имени файла — "
                    "использую имя файла",
                    path.name, t.guild_id, t.channel_id,
                )
                t.guild_id, t.channel_id = gid, cid
            self._tournaments[(gid, cid)] = t
            loaded_keys.add(key_name)

        # Manifest-сверка: пропавшие файлы — критично, лишние — info.
        if manifest:
            expected = set(manifest.get("keys", []))
            missing = expected - loaded_keys - set(self._lost_keys)
            extra = loaded_keys - expected
            for k in sorted(missing):
                self._lost_keys.append(k)
                logger.critical(
                    "КРИТИЧНО: shard %s.json отсутствует, хотя был в "
                    "манифесте — турнир потерян", k
                )
            for k in sorted(extra):
                logger.info("Shard %s.json не в манифесте — загружен", k)

        self._manifest_keys = loaded_keys
        if manifest is None:
            self._write_manifest()
        logger.info(
            "Sharded store: загружено %d турниров, потеряно %d",
            len(self._tournaments), len(self._lost_keys),
        )

    def _read_manifest(self) -> dict | None:
        raw = load_json_file(self._manifest_path)
        return raw if isinstance(raw, dict) else None

    def _write_manifest(self) -> None:
        manifest = {
            "version": 1,
            "mode": "sharded",
            "keys": sorted(self._manifest_keys),
            "count": len(self._manifest_keys),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            atomic_write_text(
                self._manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        except OSError as exc:
            logger.error("Не удалось записать manifest: %s", exc)

    def _migrate_legacy_to_shards(self) -> bool:
        """Legacy tournaments.json → shard-файлы (staging + verify +
        atomic rename + manifest). False — миграция отменена,
        legacy-файл не тронут."""
        raw = load_json_file(DATA_FILE)
        if raw is None:
            logger.error("Legacy %s отсутствует или битый", DATA_FILE)
            return False
        migrated = self._migrate(raw)
        if migrated is None:
            return False
        if not migrated:
            # Пустой legacy — просто переходим в sharded-режим.
            self._shard_dir.mkdir(parents=True, exist_ok=True)
            self._manifest_keys = set()
            self._write_manifest()
            return True

        # 1. Staging-каталог: пишем и проверяем каждый файл.
        staging = self._staging_dir
        if staging.exists():
            self._archive_incomplete(staging)
        staging.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[tuple[int, int], Tournament]] = []
        try:
            for (gid, cid), t in migrated.items():
                payload = t.to_dict()
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                path = staging / _shard_filename(gid, cid)
                atomic_write_text(path, text)
                # Проверка: файл читается и round-trip идентичен.
                back = load_json_file(path)
                if back != payload:
                    raise ValueError(f"round-trip mismatch {path.name}")
                Tournament.from_dict(back)  # структурная проверка
                staged.append(((gid, cid), t))
        except Exception as exc:
            logger.error("Миграция прервана на staging: %s", exc)
            self._archive_incomplete(staging)
            return False

        # 2. Проверка полноты: количество и уникальность ключей.
        keys = [k for k, _ in staged]
        if len(keys) != len(set(keys)) or len(keys) != len(migrated):
            logger.error("Миграция: дубликаты/неполный набор ключей")
            self._archive_incomplete(staging)
            return False

        # 3. Атомарная фиксация: rename staging → shard dir.
        target = self._shard_dir
        if target.exists() and any(
            _parse_shard_name(p) is not None for p in target.glob("*.json")
        ):
            self._archive_incomplete(target)
        elif target.exists():
            # Пустой/неполный каталог без shard-файлов — просто убрать.
            try:
                target.rmdir()
            except OSError:
                pass
        try:
            os.rename(staging, target)
        except OSError as exc:
            logger.error("Не удалось зафиксировать миграцию: %s", exc)
            self._archive_incomplete(staging)
            return False

        self._manifest_keys = {f"{g}_{c}" for (g, c), _ in staged}
        self._write_manifest()
        logger.warning(
            "Миграция завершена: %d турниров → %s (legacy %s сохранён)",
            len(staged), target, DATA_FILE.name,
        )
        return True

    def _write_shard(self, tournament: Tournament) -> None:
        """Атомарно записать ОДИН турнир в его shard-файл."""
        key = (tournament.guild_id, tournament.channel_id)
        path = self._shard_path(key)
        try:
            text = json.dumps(
                tournament.to_dict(), ensure_ascii=False, indent=2
            )
        except (TypeError, ValueError) as exc:
            logger.error("Не удалось сериализовать турнир %s: %s", key, exc)
            raise StoreUnavailableError(
                f"Не удалось сериализовать турнир {key}: {exc}"
            ) from exc
        from utils import perf_metrics

        with perf_metrics.timing("json_shard_write_ms"):
            with self._save_lock:
                rotate_backup(path)
                try:
                    atomic_write_text(path, text)
                except OSError as exc:
                    logger.error("Ошибка записи shard %s: %s", path, exc)
                    raise StoreUnavailableError(
                        f"Не удалось записать {path}: {exc}"
                    ) from exc
                # Первый shard не имеет .bak (rotate_backup копирует
                # только предыдущую версию) — сидим .bak текущим
                # содержимым: crash сразу после первой записи
                # восстановим из .bak, а не потеряем турнир.
                bak = backup_path(path)
                if not bak.exists():
                    try:
                        atomic_write_text(bak, text)
                    except OSError:
                        pass
        perf_metrics.record("json_shard_bytes", len(text))
        perf_metrics.incr("json_shard_writes")
        # Манифест: добавляем новый ключ (для существующих — без
        # перезаписи: горячий путь set() не должен писать manifest).
        kname = f"{key[0]}_{key[1]}"
        if kname not in self._manifest_keys:
            self._manifest_keys.add(kname)
            self._write_manifest()

    def _delete_shard(self, key: tuple[int, int]) -> None:
        path = self._shard_path(key)
        with self._save_lock:
            for p in (path, backup_path(path), path.with_suffix(".json.tmp")):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.error("Не удалось удалить %s: %s", p, exc)
                    raise StoreUnavailableError(
                        f"Не удалось удалить shard {key}: {exc}"
                    ) from exc
            self._manifest_keys.discard(f"{key[0]}_{key[1]}")
            self._write_manifest()

    def save_all_shards(self) -> None:
        """Перезаписать все shard-файлы из памяти (редко: shutdown,
        ручная консолидация). В sharded-режиме аналог save()."""
        if self._mode != "sharded":
            self.save()
            return
        for t in list(self._tournaments.values()):
            self._write_shard(t)
        self._manifest_keys = {
            f"{g}_{c}" for (g, c) in self._tournaments
        }
        self._write_manifest()

    # ------------------------------------------------------------------
    # Single-режим (исторический tournaments.json)
    # ------------------------------------------------------------------

    def _load_single(self) -> None:
        """Fallback: legacy-режим после неудачной миграции.

        _mode уже переведён в "single" — load() пройдёт по
        исторической ветке tournaments.json.
        """
        self.load()

    def _restore_from_backup(self) -> bool:
        """Восстановить DATA_FILE из .bak (проверив JSON и структуру).

        True — файл восстановлен и _tournaments заполнен.
        """
        bak = backup_path(DATA_FILE)
        if not bak.exists():
            return False
        raw = load_json_file(bak)
        if raw is None:
            return False
        migrated = self._migrate(raw)
        if migrated is None:
            return False
        try:
            atomic_write_text(
                DATA_FILE,
                json.dumps(raw, ensure_ascii=False, indent=2),
            )
        except OSError as exc:
            logger.error("Не удалось восстановить backup: %s", exc)
            return False
        self._tournaments = migrated
        logger.warning(
            "Восстановлен %s из резервной копии %s",
            DATA_FILE.name, bak.name,
        )
        return True

    @staticmethod
    def _migrate(raw: Any) -> dict[tuple[int, int], Tournament] | None:
        """Распарсить raw в словарь турниров. None — структура битая."""
        try:
            # Migration: convert old format {guild_id: data} to new format {(guild_id, channel_id): data}
            migrated = {}
            for key, data in raw.items():
                if isinstance(key, str) and key.isdigit():
                    # Old format: single guild_id
                    gid = int(key)
                    tournament = Tournament.from_dict(data)
                    # Use tournament's channel_id for migration
                    migrated[(gid, tournament.channel_id)] = tournament
                else:
                    # New format: "guild_id:channel_id"
                    parts = key.split(":")
                    if len(parts) == 2:
                        gid, cid = int(parts[0]), int(parts[1])
                        migrated[(gid, cid)] = Tournament.from_dict(data)
            return migrated
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            logger.error("Ошибка структуры JSON %s: %s", DATA_FILE, exc)
            return None

    def save(self) -> None:
        """Сохранить все турниры в файл (атомарно, с backup).

        В unhealthy-состоянии запись запрещена: пустое состояние
        не должно затереть повреждённые данные. Перед записью
        пробуется восстановление; если оно не удалось —
        StoreUnavailableError.

        В sharded-режиме save() = save_all_shards() — перезапись
        всех shard-файлов (set() для этого не используется).
        """
        if self._mode == "sharded":
            self.save_all_shards()
            return
        self._ensure_writable()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            from utils import perf_metrics

            with perf_metrics.timing("json_to_dict_ms"):
                raw = {
                    f"{gid}:{cid}": t.to_dict()
                    for (gid, cid), t in self._tournaments.items()
                }
            with perf_metrics.timing("json_dumps_ms"):
                text = json.dumps(raw, ensure_ascii=False, indent=2)
            perf_metrics.record("json_payload_bytes", len(text))
            perf_metrics.incr("json_saves")
        except (TypeError, ValueError) as exc:
            # Сериализация не удалась — старый файл не трогаем
            logger.error("Не удалось сериализовать турниры: %s", exc)
            raise StoreUnavailableError(
                f"Не удалось сериализовать состояние турниров: {exc}"
            ) from exc

        with self._save_lock:
            with perf_metrics.timing("json_atomic_write_ms"):
                rotate_backup(DATA_FILE)
                try:
                    atomic_write_text(DATA_FILE, text)
                except OSError as exc:
                    # .tmp мог остаться — атомарность основного файла сохранена
                    logger.error("Ошибка записи %s: %s", DATA_FILE, exc)
                    raise StoreUnavailableError(
                        f"Не удалось записать {DATA_FILE}: {exc}"
                    ) from exc

    def get(self, guild_id: int, channel_id: int) -> Tournament | None:
        """Получить турнир по guild_id и channel_id."""
        return self._tournaments.get((guild_id, channel_id))

    def set(self, tournament: Tournament) -> None:
        """Сохранить или обновить турнир.

        При недоступном хранилище выбрасывает StoreUnavailableError
        ДО изменения памяти — состояние не подтверждается ложно.
        """
        self._ensure_writable()
        self._tournaments[(tournament.guild_id, tournament.channel_id)] = tournament
        if self._mode == "sharded":
            # Пишем ТОЛЬКО изменённый турнир — O(1) вместо O(все).
            self._write_shard(tournament)
        else:
            self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить турнир по guild_id и channel_id."""
        self._ensure_writable()
        self._tournaments.pop((guild_id, channel_id), None)
        if self._mode == "sharded":
            self._delete_shard((guild_id, channel_id))
        else:
            self.save()

    def all(self) -> list[Tournament]:
        """Получить все активные турниры."""
        return list(self._tournaments.values())

    def get_by_guild(self, guild_id: int) -> list[Tournament]:
        """Получить все турниры сервера (для проверок конфликтов)."""
        return [t for (gid, cid), t in self._tournaments.items() if gid == guild_id]

    def get_settings(self) -> dict:
        """Получить настройки бота."""
        SETTINGS_FILE = DATA_DIR / "settings.json"
        raw = load_json_file(SETTINGS_FILE)
        return raw if isinstance(raw, dict) else {}

    def set_settings(self, settings: dict) -> None:
        """Сохранить настройки бота (атомарно)."""
        SETTINGS_FILE = DATA_DIR / "settings.json"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            text = json.dumps(settings, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            logger.error("Не удалось сериализовать settings: %s", exc)
            return
        with self._save_lock:
            rotate_backup(SETTINGS_FILE)
            try:
                atomic_write_text(SETTINGS_FILE, text)
            except OSError as exc:
                logger.error("Ошибка записи %s: %s", SETTINGS_FILE, exc)


def export_shards_to_legacy(data_dir: Path | None = None) -> Path:
    """Безопасный rollback: экспортировать shard-файлы в единый
    legacy tournaments.json.

    Читает shard-файлы с диска (не память), проверяет каждый
    через Tournament.from_dict, сверяет количество, затем атомарно
    пишет legacy-файл. После этого можно выставить
    JSON_STORE_MODE=single — legacy будет актуальным.

    Возвращает путь записанного файла. При любой неполадке —
    StoreUnavailableError, legacy-файл не трогается.
    """
    base = Path(data_dir) if data_dir else DATA_DIR
    shard_dir = base / SHARD_DIR_NAME
    legacy = base / DATA_FILE.name
    tournaments: dict[tuple[int, int], Tournament] = {}

    for path in sorted(shard_dir.glob("*.json")):
        parsed = _parse_shard_name(path)
        if parsed is None:
            continue
        gid, cid = parsed
        raw = load_json_file(path)
        if raw is None:
            raw = load_json_file(backup_path(path))
        if raw is None:
            raise StoreUnavailableError(
                f"Shard {path.name} повреждён — rollback отменён, "
                "legacy-файл не перезаписан"
            )
        try:
            t = Tournament.from_dict(raw)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise StoreUnavailableError(
                f"Shard {path.name} не парсится ({exc}) — rollback "
                "отменён"
            ) from exc
        tournaments[(gid, cid)] = t

    # Сверка с манифестом (если есть) — не должно быть пропусков.
    manifest_path = base / MANIFEST_NAME
    manifest = load_json_file(manifest_path)
    if isinstance(manifest, dict):
        expected = set(manifest.get("keys", []))
        actual = {f"{g}_{c}" for (g, c) in tournaments}
        missing = expected - actual
        if missing:
            raise StoreUnavailableError(
                f"Отсутствуют shard-файлы {sorted(missing)} — "
                "rollback отменён, экспорт был бы неполным"
            )

    payload = {
        f"{gid}:{cid}": t.to_dict()
        for (gid, cid), t in tournaments.items()
    }
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise StoreUnavailableError(
            f"Не удалось сериализовать состояние: {exc}"
        ) from exc

    # Предварительная проверка: каждый турнир переживает round-trip.
    for key, data in payload.items():
        Tournament.from_dict(data)

    atomic_write_text(legacy, text)
    logger.warning(
        "Rollback: экспортировано %d турниров в %s",
        len(tournaments), legacy,
    )
    return legacy


# Глобальный экземпляр хранилища
store = JsonStore()
