# ruff: noqa: B008
"""API каталогов контента событийных игр (Events Content
Expansion): «Перепутанное слово» и «Крокодил».

Управление — ТОЛЬКО BOT_OWNER_ID (как shop catalog): Discord
Administrator и модераторы панели прав не имеют. Записи
хранятся в PostgreSQL (event_content_*), bundled JSON —
базовая версия. Правильные ответы (answer/accepted/word)
никогда не отдаются публичным маршрутам бота — этот API
доступен только аутентифицированному владельцу панели.

Импорт: POST /{game}/import принимает {"entries": [...],
"dry_run": bool, "version": str, "replace": bool}.
Валидация — services.event_content.validate_catalog;
при ошибках записи не происходит (атомарный отказ), dry_run
возвращает список ошибок с индексами записей.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from control_center import rbac
from control_center.auth import (
    PanelUser,
    current_user,
    require_perm,
    verify_csrf,
)
from services import event_content
from storage import admin_config_store
from storage.event_content_store import event_content_store

router = APIRouter(prefix="/api/event-content", tags=["event-content"])

GAMES = ("word", "krokodil")


async def _require_owner(user: PanelUser = Depends(current_user)
                         ) -> PanelUser:
    """Контент заданий — ТОЛЬКО владелец бота (как shop)."""
    try:
        from config import BOT_OWNER_ID, GUILD_ID
        from control_center.config import PANEL_GUILD_ID
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=403,
            detail="event content management is restricted "
                   "to bot owner")
    if int(BOT_OWNER_ID or 0) != user.id:
        raise HTTPException(status_code=403, detail="bot owner only")
    expected = PANEL_GUILD_ID or int(GUILD_ID or 0)
    if expected and user.guild_id and user.guild_id != expected:
        raise HTTPException(status_code=403, detail="wrong guild")
    return user


def _game(game: str) -> str:
    if game not in GAMES:
        raise HTTPException(404, f"unknown game {game!r}")
    return game


async def _audit(user: PanelUser, action: str, game: str,
                 object_id: str = "", old: Any = None,
                 new: Any = None) -> None:
    try:
        await admin_config_store.insert_audit(
            guild_id=user.guild_id or 0, actor_id=user.id,
            action=action, object_type="event_content",
            object_id=f"{game}:{object_id}",
            old_value=old, new_value=new)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "event_content audit write failed", exc_info=True)


class EntryBody(BaseModel):
    id: str | None = Field(default=None, max_length=100)
    # word
    word: str | None = Field(default=None, max_length=50)
    # krokodil
    answer: str | None = Field(default=None, max_length=200)
    clue: str | None = Field(default=None, max_length=1000)
    accepted: list[str] | None = None
    # общие
    category: str = Field(default="", max_length=100)
    difficulty: str = "easy"
    enabled: bool = True


class ImportBody(BaseModel):
    entries: list[dict[str, Any]] = Field(default_factory=list)
    version: str = Field(default="", max_length=100)
    replace: bool = True
    dry_run: bool = False


@router.get("/games")
async def list_games(
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    """Сводка по каталогам: версии, счётчики, bundled-fallback."""
    out = []
    for game in GAMES:
        stats = await event_content_store.catalog_stats(game)
        bundled_n = len(event_content.bundled_entries(game))
        out.append({**stats, "bundled_entries": bundled_n,
                    "bundled_version":
                        event_content.bundled_version(game)})
    return {"games": out}


@router.get("/{game}/entries")
async def list_entries(
    game: str,
    q: str = "",
    difficulty: str = "",
    category: str = "",
    enabled: bool | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    game = _game(game)
    items, total = await event_content_store.list_entries(
        game, q=q.strip(), difficulty=difficulty,
        category=category, enabled=enabled,
        limit=limit, offset=offset)
    return {"items": items, "total": total,
            "limit": limit, "offset": offset}


@router.get("/{game}/categories")
async def list_categories(
    game: str,
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    return {"categories":
            await event_content_store.list_categories(_game(game))}


@router.get("/{game}/entries/{entry_id}")
async def get_entry(
    game: str, entry_id: str,
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    item = await event_content_store.get_entry(_game(game), entry_id)
    if item is None:
        raise HTTPException(404, "entry not found")
    return item


@router.post("/{game}/entries")
async def upsert_entry(
    game: str, body: EntryBody,
    user: PanelUser = Depends(_require_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    game = _game(game)
    entry = _entry_from_body(body, game)
    pseudo = {"schema_version": 1, "content_version": "panel",
              "locale": "ru", "game": game, "entries": [entry]}
    errors = event_content.validate_catalog(pseudo, game)
    if errors:
        raise HTTPException(422, detail="; ".join(errors[:10]))
    eid, op = await event_content_store.upsert_entry(
        game, entry, actor=str(user.id))
    await _audit(user, f"event_content.{op}", game, eid,
                 new={k: v for k, v in entry.items()
                      if k not in ("clue", "answer", "accepted",
                                   "word")})
    return {"id": eid, "status": op}


@router.patch("/{game}/entries/{entry_id}")
async def patch_entry(
    game: str, entry_id: str,
    body: dict[str, Any],
    user: PanelUser = Depends(_require_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    """Частичное обновление: enabled/weight/difficulty/category
    и контентные поля (clue/word/answer/accepted)."""
    game = _game(game)
    current = await event_content_store.get_entry(game, entry_id)
    if current is None:
        raise HTTPException(404, "entry not found")
    allowed = {"enabled", "difficulty", "category", "clue",
               "word", "answer", "accepted"}
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        raise HTTPException(422, "nothing to update")
    merged = {**current, **patch, "id": entry_id}
    pseudo = {"schema_version": 1, "content_version": "panel",
              "locale": "ru", "game": game, "entries": [merged]}
    errors = event_content.validate_catalog(pseudo, game)
    if errors:
        raise HTTPException(422, detail="; ".join(errors[:10]))
    # upsert атомарнее раздельного patch колонок+payload:
    # пишем объединённую запись целиком.
    entry = dict(current)
    entry.update(patch)
    entry["id"] = entry_id
    await event_content_store.upsert_entry(
        game, entry, actor=str(user.id))
    await _audit(user, "event_content.updated", game, entry_id,
                 new={k: ("***" if k in ("answer", "accepted",
                                         "word") else v)
                      for k, v in patch.items()})
    return {"id": entry_id, "status": "updated"}


@router.delete("/{game}/entries/{entry_id}")
async def delete_entry(
    game: str, entry_id: str,
    user: PanelUser = Depends(_require_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    game = _game(game)
    ok = await event_content_store.delete_entry(game, entry_id)
    if not ok:
        raise HTTPException(404, "entry not found")
    await _audit(user, "event_content.deleted", game, entry_id)
    return {"id": entry_id, "status": "deleted"}


@router.post("/{game}/entries/{entry_id}/toggle")
async def toggle_entry(
    game: str, entry_id: str,
    user: PanelUser = Depends(_require_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    game = _game(game)
    item = await event_content_store.get_entry(game, entry_id)
    if item is None:
        raise HTTPException(404, "entry not found")
    entry = dict(item)
    entry["enabled"] = not bool(item.get("enabled", True))
    await event_content_store.upsert_entry(
        game, entry, actor=str(user.id))
    await _audit(user, "event_content.toggled", game, entry_id,
                 new={"enabled": entry["enabled"]})
    return {"id": entry_id, "enabled": entry["enabled"]}


@router.get("/{game}/export")
async def export_catalog(
    game: str,
    user: PanelUser = Depends(_require_owner),
) -> dict[str, Any]:
    """Экспорт рабочего каталога в формате bundled JSON."""
    game = _game(game)
    entries = await event_content_store.export_entries(game)
    stats = await event_content_store.catalog_stats(game)
    return {
        "schema_version": 1,
        "content_version": stats.get("content_version") or "export",
        "locale": "ru",
        "game": game,
        "entries": entries,
    }


@router.post("/{game}/import")
async def import_catalog(
    game: str, body: ImportBody,
    user: PanelUser = Depends(_require_owner),
    _csrf: None = Depends(verify_csrf),
) -> dict[str, Any]:
    """Импорт/замена каталога. dry_run — только валидация.
    Невалидный импорт не меняет рабочий каталог."""
    game = _game(game)
    pseudo = {"schema_version": 1,
              "content_version": body.version or "import",
              "locale": "ru", "game": game,
              "entries": body.entries}
    errors = event_content.validate_catalog(pseudo, game)
    if body.dry_run:
        preview = await _import_preview(game, body.entries,
                                        body.replace)
        return {"valid": not errors, "errors": errors[:100],
                "entries": len(body.entries), "preview": preview}
    if errors:
        await _audit(user, "event_content.import_rejected", game,
                     new={"errors": errors[:20]})
        raise HTTPException(
            422, detail={"message": "validation failed",
                         "errors": errors[:100]})
    result = await event_content_store.import_entries(
        game, body.entries, version=body.version or "import",
        actor=str(user.id), replace=body.replace)
    await _audit(user, "event_content.imported", game,
                 new={**result, "version": body.version})
    return {"status": "imported", **result}


@router.get("/{game}/state")
async def cycle_state(
    game: str, guild_id: str = "default",
    user: PanelUser = Depends(require_perm(rbac.PERM_SETTINGS_VIEW)),
) -> dict[str, Any]:
    game = _game(game)
    return {"state": await event_content_store.state(game,
                                                     guild_id),
            "recent_draws": await event_content_store.recent_draws(
                game, guild_id, limit=20)}


def _entry_from_body(body: EntryBody, game: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": body.id or "",
        "category": body.category,
        "difficulty": body.difficulty,
        "enabled": body.enabled,
    }
    if game == "word":
        if not body.word:
            raise HTTPException(422, "word is required")
        entry["word"] = body.word.strip().lower()
        if body.accepted:
            entry["accepted"] = body.accepted
    else:
        if not body.answer or not body.clue:
            raise HTTPException(422,
                                "answer and clue are required")
        entry["answer"] = body.answer.strip()
        entry["clue"] = body.clue.strip()
        entry["accepted"] = body.accepted or [body.answer.strip()]
    return entry


async def _import_preview(game: str, entries: list[dict],
                          replace: bool) -> dict[str, Any]:
    """Preview изменений импорта: новые/изменённые/удаляемые
    id относительно текущего каталога (без записи)."""
    try:
        current = {e.get("id"): e
                   for e in await event_content_store
                   .export_entries(game)}
    except Exception:  # noqa: BLE001
        return {"error": "current catalog unavailable"}
    incoming = {e.get("id"): e for e in entries
                if isinstance(e, dict) and e.get("id")}
    new_ids = sorted(set(incoming) - set(current))
    removed_ids = sorted(set(current) - set(incoming)) \
        if replace else []

    def _norm(e: dict) -> str:
        import json as _j
        return _j.dumps(e, ensure_ascii=False, sort_keys=True)

    changed_ids = sorted(
        i for i in set(incoming) & set(current)
        if _norm(incoming[i]) != _norm(current[i]))
    return {
        "current_total": len(current),
        "incoming_total": len(incoming),
        "new": len(new_ids), "new_ids": new_ids[:50],
        "changed": len(changed_ids),
        "changed_ids": changed_ids[:50],
        "removed": len(removed_ids),
        "removed_ids": removed_ids[:50],
        "replace": replace,
    }
