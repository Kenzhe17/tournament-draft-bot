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


class JsonStore:
    """Сохранение и загрузка турниров в JSON-файл."""

    def __init__(self) -> None:
        self._tournaments: dict[tuple[int, int], Tournament] = {}
        self._save_lock = threading.Lock()
        self._unhealthy = False
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def is_healthy(self) -> bool:
        """False — хранилище повреждено и требует ручного восстановления."""
        return not self._unhealthy

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
        """Загрузить все турниры из файла.

        Файла нет — первый запуск, пустое состояние (healthy).
        Файл битый — оригинал в *.corrupt-<ts>, пробуем .bak.
        Нет пригодных копий — unhealthy: данные не перезаписываются.
        При успешной загрузке unhealthy-флаг сбрасывается
        (self-recovery после ручного восстановления файла).
        """
        self._unhealthy = False
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
        """
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
        self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить турнир по guild_id и channel_id."""
        self._ensure_writable()
        self._tournaments.pop((guild_id, channel_id), None)
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


# Глобальный экземпляр хранилища
store = JsonStore()
