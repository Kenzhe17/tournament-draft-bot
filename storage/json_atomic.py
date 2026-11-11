"""Общие атомарные операции записи JSON-файлов.

atomic_write_text: tmp + flush + fsync + os.replace — читатели никогда
не видят частично записанный файл. После os.replace на Linux
выполняется fsync каталога для устойчивости к системному сбою.

rotate_backup: предыдущая версия файла сохраняется в <name>.bak
атомарно и только если текущий файл — валидный JSON (битый файл
не должен затирать последнюю хорошую копию).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def fsync_dir(path: Path) -> None:
    """fsync каталога — best effort (на Windows может не поддерживаться)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Атомарно записать text в path (tmp + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any, *, indent: int | None = 2) -> None:
    """Атомарно записать obj как JSON (ensure_ascii=False)."""
    text = json.dumps(obj, ensure_ascii=False, indent=indent)
    atomic_write_text(path, text)


def load_json_file(path: Path) -> Any | None:
    """Прочитать и распарсить JSON. None — файла нет или битый."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.error("Файл %s повреждён или нечитаем: %s", path, exc)
        return None


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def rotate_backup(path: Path) -> bool:
    """Атомарно обновить <name>.bak из текущего файла.

    Бэкап создаётся только если существующий файл — валидный JSON:
    повреждённый файл не должен затирать последнюю хорошую копию.
    Возвращает True, если backup обновлён.
    """
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        json.loads(text)  # проверка целостности до записи backup
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "Backup %s не обновлён — текущий файл невалиден: %s",
            backup_path(path).name, exc,
        )
        return False
    try:
        atomic_write_text(backup_path(path), text)
        return True
    except OSError as exc:
        logger.warning("Не удалось создать backup %s: %s", path, exc)
        return False
