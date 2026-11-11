"""JSON-хранилище состояния турниров.

Атомарная запись: данные сериализуются в строку, пишутся во временный
файл в той же директории, fsync, затем os.replace() — читатели никогда
не видят частично записанный файл. Предыдущая версия сохраняется в
<name>.bak; при загрузке повреждённого файла оригинал откладывается в
<name>.corrupt-<ts>, а при наличии валидного .bak он восстанавливается.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import DATA_DIR, DATA_FILE
from models.tournament import Tournament

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _atomic_write_text(path: Path, text: str) -> None:
    """Атомарно записать text в path: tmp + flush + fsync + os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _corrupt_path(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.corrupt-{ts}")


def _rotate_backup(path: Path) -> None:
    """Сохранить текущую версию файла как .bak перед перезаписью."""
    if not path.exists():
        return
    try:
        shutil.copyfile(path, _backup_path(path))
    except OSError as exc:
        logger.warning("Не удалось создать backup %s: %s", path, exc)


def _load_json_file(path: Path) -> Any | None:
    """Прочитать и распарсить JSON. None — файла нет или битый."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.error("Файл %s повреждён или нечитаем: %s", path, exc)
        return None


class JsonStore:
    """Сохранение и загрузка турниров в JSON-файл."""

    def __init__(self) -> None:
        self._tournaments: dict[tuple[int, int], Tournament] = {}
        self._save_lock = threading.Lock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить все турниры из файла.

        При битом JSON: оригинал сохраняется как *.corrupt-<ts>,
        затем пробуется *.bak — валидная копия восстанавливается
        в основной файл. Пустой словарь — только если нет ни одного
        пригодного источника.
        """
        if not DATA_FILE.exists():
            self._tournaments = {}
            return

        raw = _load_json_file(DATA_FILE)
        if raw is None:
            # Сохраняем повреждённый файл для диагностики
            try:
                os.replace(DATA_FILE, _corrupt_path(DATA_FILE))
            except OSError as exc:
                logger.error("Не удалось отложить битый файл: %s", exc)
            # Пробуем backup
            bak = _backup_path(DATA_FILE)
            if bak.exists():
                raw = _load_json_file(bak)
                if raw is not None:
                    try:
                        _atomic_write_text(
                            DATA_FILE,
                            json.dumps(raw, ensure_ascii=False, indent=2),
                        )
                        logger.warning(
                            "Восстановлен %s из резервной копии %s",
                            DATA_FILE.name, bak.name,
                        )
                    except OSError as exc:
                        logger.error("Не удалось восстановить backup: %s", exc)
            if raw is None:
                logger.error(
                    "Нет пригодной копии %s — старт с пустым состоянием",
                    DATA_FILE.name,
                )
                self._tournaments = {}
                return

        migrated = self._migrate(raw)
        if migrated is None:
            # Структура не читается — пробуем backup, как при битом JSON
            bak = _backup_path(DATA_FILE)
            if bak.exists():
                bak_raw = _load_json_file(bak)
                if bak_raw is not None:
                    migrated = self._migrate(bak_raw)
                    if migrated is not None:
                        try:
                            _atomic_write_text(
                                DATA_FILE,
                                json.dumps(bak_raw, ensure_ascii=False,
                                           indent=2),
                            )
                            logger.warning(
                                "Восстановлен %s из резервной копии %s",
                                DATA_FILE.name, bak.name,
                            )
                        except OSError as exc:
                            logger.error("Не удалось восстановить backup: %s", exc)
            if migrated is None:
                logger.error(
                    "Нет пригодной копии %s — старт с пустым состоянием",
                    DATA_FILE.name,
                )
                self._tournaments = {}
                return

        self._tournaments = migrated
        logger.info("Загружено %d турниров", len(self._tournaments))

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
        """Сохранить все турниры в файл (атомарно, с backup)."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            raw = {
                f"{gid}:{cid}": t.to_dict()
                for (gid, cid), t in self._tournaments.items()
            }
            text = json.dumps(raw, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            # Сериализация не удалась — старый файл не трогаем
            logger.error("Не удалось сериализовать турниры: %s", exc)
            return

        with self._save_lock:
            _rotate_backup(DATA_FILE)
            try:
                _atomic_write_text(DATA_FILE, text)
            except OSError as exc:
                # .tmp мог остаться — атомарность основного файла сохранена
                logger.error("Ошибка записи %s: %s", DATA_FILE, exc)

    def get(self, guild_id: int, channel_id: int) -> Tournament | None:
        """Получить турнир по guild_id и channel_id."""
        return self._tournaments.get((guild_id, channel_id))

    def set(self, tournament: Tournament) -> None:
        """Сохранить или обновить турнир."""
        self._tournaments[(tournament.guild_id, tournament.channel_id)] = tournament
        self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Удалить турнир по guild_id и channel_id."""
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
        raw = _load_json_file(SETTINGS_FILE)
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
            _rotate_backup(SETTINGS_FILE)
            try:
                _atomic_write_text(SETTINGS_FILE, text)
            except OSError as exc:
                logger.error("Ошибка записи %s: %s", SETTINGS_FILE, exc)


# Глобальный экземпляр хранилища
store = JsonStore()
