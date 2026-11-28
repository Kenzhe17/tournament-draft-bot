"""Каталоги заданий событийных игр «Перепутанное слово» и
«Крокодил» (Events Content Expansion).

Bundled-ресурсы ``content/events/{words,krokodil}_ru.json``
внутри пакета — доступны в Docker-образе независимо от
рабочего каталога. Загружаются через importlib.resources,
валидируются по схеме; при ошибке — безопасный fallback на
последнюю валидную bundled-версию (каталог не может «уронить»
бота: пустой/невалидный источник → игра недоступна для выбора).

Рабочая (редактируемая через Control Center) копия живёт в
PostgreSQL — ``storage/event_content_store``. Этот модуль —
только bundled-загрузка и чистая нормализация ответов.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from functools import cache
from typing import Any

logger = logging.getLogger(__name__)

CONTENT_PKG = "content.events"
GAME_FILES = {
    "word": "words_ru.json",
    "krokodil": "krokodil_ru.json",
}
GAMES = tuple(GAME_FILES)
DIFFICULTIES = ("easy", "medium", "hard")
CATEGORIES_WORD = (
    "Быт и предметы", "Еда и напитки", "Животные",
    "Наука и культура", "Природа и география",
    "Профессии и общество", "Растения и сад",
    "Спорт и игры", "Технологии и интернет",
    "Транспорт и город",
)

_SCHEMA_KEYS = {"schema_version", "content_version", "locale",
                "game", "entries"}


def _import_file(filename: str):
    """importlib.resources — работает и из wheel, и из каталога."""
    from importlib import resources
    try:
        return resources.files(CONTENT_PKG).joinpath(filename).open(
            "r", encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        # content/ — пакет верхнего уровня репозитория; fallback
        # на файловый путь рядом с корнем проекта.
        import pathlib
        p = pathlib.Path(__file__).resolve().parents[1] \
            / "content" / "events" / filename
        return p.open("r", encoding="utf-8")


def validate_catalog(data: Any, game: str) -> list[str]:
    """Структурная валидация каталога. Возвращает список ошибок
    (пустой = валиден)."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["catalog is not an object"]
    if not _SCHEMA_KEYS <= set(data):
        errors.append("missing root keys")
    if data.get("game") != game:
        errors.append(f"game mismatch: {data.get('game')!r}")
    if data.get("locale") != "ru":
        errors.append("locale must be 'ru'")
    entries = data.get("entries")
    if not isinstance(entries, list):
        return errors + ["entries is not a list"]
    seen_ids: set[str] = set()
    seen_norm: set[str] = set()
    seen_clues: set[str] = set()
    for idx, e in enumerate(entries):
        if isinstance(e, dict) and e.get("id"):
            tag = f"entries[{idx}]({e['id']})"
        else:
            tag = f"entries[{idx}]"
        if not isinstance(e, dict):
            errors.append(f"{tag}: not an object")
            continue
        eid = e.get("id")
        if not isinstance(eid, str) or not eid:
            errors.append(f"{tag}: missing id")
        elif eid in seen_ids:
            errors.append(f"{tag}: duplicate id {eid}")
        else:
            seen_ids.add(eid)
        if e.get("enabled") is not True and e.get("enabled") is not False:
            errors.append(f"{tag}: enabled must be bool")
        if e.get("difficulty") not in DIFFICULTIES:
            errors.append(f"{tag}: bad difficulty {e.get('difficulty')!r}")
        if not e.get("category"):
            errors.append(f"{tag}: missing category")
        if game == "word":
            w = e.get("word")
            if not isinstance(w, str) or not w:
                errors.append(f"{tag}: missing word")
            else:
                n = normalize_word_key(w)
                if n != normalize_word_key(w.strip()):
                    errors.append(f"{tag}: word not normalized {w!r}")
                if len(w) < 4 or len(w) > 15:
                    errors.append(f"{tag}: word length {len(w)}")
                if not all("а" <= ch.lower() <= "я" or ch == "ё"
                           for ch in w):
                    errors.append(f"{tag}: non-cyrillic word {w!r}")
                if " " in w or "-" in w:
                    errors.append(f"{tag}: word has space/hyphen {w!r}")
                if n in seen_norm:
                    errors.append(f"{tag}: duplicate word {w!r}")
                seen_norm.add(n)
        else:
            ans = e.get("answer")
            clue = e.get("clue")
            acc = e.get("accepted")
            if not isinstance(ans, str) or not ans.strip():
                errors.append(f"{tag}: missing answer")
            if not isinstance(clue, str) or len(clue.strip()) < 10:
                errors.append(f"{tag}: missing/short clue")
            if not isinstance(acc, list) or not acc or \
                    not all(isinstance(a, str) and a.strip()
                            for a in acc):
                errors.append(f"{tag}: accepted must be non-empty "
                              "string list")
            elif normalize_answer(ans) != normalize_answer(acc[0]):
                errors.append(f"{tag}: accepted[0] != answer")
            if isinstance(ans, str) and isinstance(clue, str):
                na = normalize_answer(ans)
                nc = normalize_answer(clue)
                if na and na in nc:
                    errors.append(f"{tag}: clue contains answer")
                if nc in seen_clues:
                    errors.append(f"{tag}: duplicate clue")
                seen_clues.add(nc)
            if isinstance(acc, list):
                for a in acc:
                    na = normalize_answer(a)
                    if na in seen_norm:
                        errors.append(
                            f"{tag}: duplicate accepted {a!r}")
                    seen_norm.add(na)
                    if isinstance(clue, str) and na and \
                            na in normalize_answer(clue):
                        errors.append(
                            f"{tag}: clue contains alias {a!r}")
    return errors


def _load_json_file(filename: str) -> dict[str, Any] | None:
    try:
        with _import_file(filename) as fh:
            return json.load(fh)
    except Exception:
        logger.exception("event content load failed: %s", filename)
        return None


@cache
def bundled_catalog(game: str) -> dict[str, Any] | None:
    """Bundled-каталог игры или None при ошибке/невалидности.
    Кэшируется — файл статичен внутри образа."""
    if game not in GAME_FILES:
        return None
    data = _load_json_file(GAME_FILES[game])
    if data is None:
        return None
    errors = validate_catalog(data, game)
    if errors:
        logger.error("bundled catalog %s invalid: %s",
                     GAME_FILES[game], errors[:10])
        return None
    return data


def bundled_entries(game: str) -> list[dict[str, Any]]:
    data = bundled_catalog(game)
    if not data:
        return []
    return [dict(e) for e in data.get("entries") or []
            if isinstance(e, dict)]


def bundled_version(game: str) -> str:
    data = bundled_catalog(game)
    return str((data or {}).get("content_version") or "")


# ---------------------------------------------------------- нормализация

def normalize_answer(text: str) -> str:
    """Ответ игрока: NFC, lowercase, ё→е, внешние пробелы,
    только буквы/цифры/пробелы. Внутренние символы НЕ
    удаляютсяпроизвольно — дефисы/пробелы составных ответов
    нормализуются к пробелу."""
    t = unicodedata.normalize("NFC", text or "")
    t = t.strip().lower().replace("ё", "е")
    out = []
    for ch in t:
        if ch.isalnum():
            out.append(ch)
        elif ch in "-–—_" or ch.isspace():
            out.append(" ")
    return " ".join("".join(out).split())


def normalize_word_key(word: str) -> str:
    """Ключ слова для дедупликации/сравнения."""
    return normalize_answer(word).replace(" ", "")


def answer_matches(text: str, accepted: list[str] | tuple[str, ...]
                   ) -> bool:
    """Точное совпадение после нормализации — без fuzzy."""
    norm = normalize_answer(text)
    if not norm:
        return False
    return any(norm == normalize_answer(a) for a in accepted)


def scramble_word(word: str, rng) -> str:
    """Перемешать буквы слова: тот же мультимножество букв
    (включая повторяющиеся и ё), результат != оригиналу."""
    letters = list(word)
    for _ in range(30):
        rng.shuffle(letters)
        s = "".join(letters)
        if s != word:
            return s
    return word[::-1]
