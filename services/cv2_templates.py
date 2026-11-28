"""Безопасный рендерер CV2-шаблонов для Control Center.

Шаблон = JSON-payload в admin_templates. Рендерер допускает только
декларативные блоки; custom_id и callback-обработчики НЕ создаются —
единственный тип кнопки — link (url), который не требует обработчика.

Структура payload:
{
  "title": "...",                # необязательно, <= 80 симв
  "blocks": [
    {"type": "heading", "text": "..."},
    {"type": "text", "text": "..."},
    {"type": "separator", "large": true},
    {"type": "image", "url": "https://..."},
    {"type": "fields", "fields": [{"name": "..", "value": ".."}]},
    {"type": "button_link", "label": "..", "url": "https://..."}
  ]
}
"""

from __future__ import annotations

from typing import Any

import discord

MAX_BLOCKS = 40
MAX_TEXT = 4000          # лимит TextDisplay Discord
MAX_TITLE = 80
MAX_FIELDS = 25
ALLOWED_TYPES = {
    "heading", "text", "separator", "image", "fields", "button_link",
}

# Поддерживаемые ключи шаблонов (подсказка для UI; ключ свободный,
# но renderer валидирует payload).
SUPPORTED_KEYS = [
    "welcome.card",
    "tournament.announcement",
    "economy.receipt",
    "moderation.notice",
    "custom.message",
]


class TemplateError(ValueError):
    pass


def _s(v: Any, limit: int) -> str:
    return str(v or "")[:limit]


def validate_payload(payload: dict[str, Any]) -> None:
    """Валидация payload до сохранения/рендера (422-уровень)."""
    if not isinstance(payload, dict):
        raise TemplateError("payload must be an object")
    if "custom_id" in payload or "handler" in payload:
        raise TemplateError("custom_id/handlers are not allowed")
    blocks = payload.get("blocks", [])
    if not isinstance(blocks, list):
        raise TemplateError("blocks must be a list")
    if len(blocks) > MAX_BLOCKS:
        raise TemplateError(f"too many blocks (max {MAX_BLOCKS})")
    title = payload.get("title")
    if title is not None and len(str(title)) > MAX_TITLE:
        raise TemplateError(f"title too long (max {MAX_TITLE})")
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            raise TemplateError(f"block {i} must be an object")
        t = b.get("type")
        if t not in ALLOWED_TYPES:
            raise TemplateError(f"block {i}: unknown type {t!r}")
        if t in ("heading", "text") and not str(b.get("text", "")).strip():
            raise TemplateError(f"block {i}: empty text")
        if t == "image" and not str(b.get("url", "")).startswith(
                ("https://", "http://")):
            raise TemplateError(f"block {i}: image url must be http(s)")
        if t == "button_link":
            url = str(b.get("url", ""))
            if not url.startswith(("https://", "http://")):
                raise TemplateError(
                    f"block {i}: button url must be http(s)")
            if not str(b.get("label", "")).strip():
                raise TemplateError(f"block {i}: button label required")
        if t == "fields":
            fields = b.get("fields", [])
            if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
                raise TemplateError(
                    f"block {i}: fields must be list <= {MAX_FIELDS}")
            for f in fields:
                if not isinstance(f, dict) or not f.get("name") \
                        or not f.get("value"):
                    raise TemplateError(
                        f"block {i}: field needs name+value")


def render_template(payload: dict[str, Any]) -> discord.ui.LayoutView:
    """Отрендерить payload в discord.ui.LayoutView (Components V2)."""
    from utils.design.layouts import DesignView

    validate_payload(payload)
    view = DesignView(timeout=None)
    title = payload.get("title")
    if title:
        view.text(f"> ### {_s(title, MAX_TITLE)}")
        view.sep()

    pending_row: list[discord.ui.Item] = []

    def flush_row() -> None:
        nonlocal pending_row
        if pending_row:
            view.row(*pending_row)
            pending_row = []

    for b in payload.get("blocks", []):
        t = b["type"]
        if t == "heading":
            flush_row()
            view.text(f"> ## {_s(b['text'], 256)}")
        elif t == "text":
            flush_row()
            view.text(_s(b["text"], MAX_TEXT))
        elif t == "separator":
            flush_row()
            view.sep(large=bool(b.get("large")))
        elif t == "image":
            flush_row()
            view.component(discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=str(b["url"]))))
        elif t == "fields":
            flush_row()
            for f in b["fields"][:MAX_FIELDS]:
                view.text(
                    f"**{_s(f['name'], 256)}**\n{_s(f['value'], MAX_TEXT)}")
        elif t == "button_link":
            btn = discord.ui.Button(
                label=_s(b["label"], 80),
                style=discord.ButtonStyle.link,
                url=str(b["url"]))
            pending_row.append(btn)
            if len(pending_row) >= 5:
                flush_row()
    flush_row()
    return view


def preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Текстовый preview для UI (без discord-объектов)."""
    validate_payload(payload)
    lines = []
    if payload.get("title"):
        lines.append(f"### {payload['title']}")
    for b in payload.get("blocks", []):
        t = b["type"]
        if t == "heading":
            lines.append(f"## {b['text']}")
        elif t == "text":
            lines.append(str(b["text"]))
        elif t == "separator":
            lines.append("———")
        elif t == "image":
            lines.append(f"[image] {b['url']}")
        elif t == "fields":
            for f in b["fields"]:
                lines.append(f"**{f['name']}**: {f['value']}")
        elif t == "button_link":
            lines.append(f"[ {b['label']} ]({b['url']})")
    return {"lines": lines, "block_count": len(payload.get("blocks", []))}
