"""Централизованный debounce/coalescing для message.edit().

Проблема: динамические сообщения (панель очереди MM, карточки
Ready Check, турнирные статусы) редактируются на каждое событие —
серия кликов порождает параллельные PATCH к одному message_id
и Discord API отвечает 429.

Решение: на каждый ключ (message_id/логический id) — один
воркер с окном debounce. Все вызовы schedule() в окне
схлопываются: builder читает актуальное состояние на момент
выполнения, поэтому устаревшие правки не публикуются.

Гарантии:
- максимум один активный edit на ключ;
- правки, пришедшие во время активного edit, не теряются
  (worker делает ещё один проход);
- PATCH не отправляется, если сериализованное содержимое
  не изменилось (fingerprint);
- discord.NotFound → on_missing() один раз, ключ снимается,
  повторных запросов к удалённому сообщению нет;
- очередь ограничена (max_keys), старейшие pending выкидываются;
- 429 обрабатывается discord.py HTTPClient (retry_after),
  плюс не более одной повторной попытки на edit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import discord

logger = logging.getLogger("utils.message_refresher")


@dataclass
class EditRequest:
    """Запрос на редактирование, который вернул builder."""

    message: discord.Message
    kwargs: dict[str, Any]
    on_missing: Optional[Callable[[], Awaitable[None]]] = None


Builder = Callable[[], Awaitable[Optional[EditRequest]]]


def _component_fingerprint(item: Any) -> Any:
    """Стабильная сериализация UI-компонента для diff."""
    if item is None:
        return None
    # TextDisplay / Section / Container-подобные с .content
    content = getattr(item, "content", None)
    label = getattr(item, "label", None)
    cid = getattr(item, "custom_id", None)
    out: dict[str, Any] = {
        "t": type(item).__name__,
        "label": label,
        "cid": cid,
        "disabled": bool(getattr(item, "disabled", False)),
        "style": str(getattr(item, "style", "")),
        "content": content if isinstance(content, str) else None,
    }
    emoji = getattr(item, "emoji", None)
    if emoji is not None:
        out["emoji"] = str(emoji)
    ph = getattr(item, "placeholder", None)
    if ph:
        out["placeholder"] = ph
    options = getattr(item, "options", None)
    if options:
        out["options"] = [
            {
                "l": getattr(o, "label", None),
                "v": getattr(o, "value", None),
                "d": bool(getattr(o, "default", False)),
            }
            for o in options
        ]
    children = getattr(item, "children", None)
    if children:
        out["children"] = [_component_fingerprint(c) for c in children]
    return out


def payload_fingerprint(kwargs: dict[str, Any]) -> str:
    """Стабильный хэш содержимого edit-вызова.

    embeds → to_dict(), view → рекурсивный обход компонентов,
    content/allowed_mentions → строки. Одинаковое содержимое
    даёт одинаковый хэш → PATCH можно пропустить.
    """
    norm: dict[str, Any] = {}
    for k in sorted(kwargs):
        v = kwargs[k]
        if k == "embed" and v is not None and hasattr(v, "to_dict"):
            norm[k] = v.to_dict()
        elif k == "embeds" and v is not None:
            norm[k] = [
                e.to_dict() if hasattr(e, "to_dict") else str(e) for e in v
            ]
        elif k == "view" and v is not None:
            children = getattr(v, "children", None)
            norm[k] = (
                [_component_fingerprint(c) for c in children]
                if children is not None else str(type(v).__name__)
            )
        elif k == "allowed_mentions" and v is not None:
            norm[k] = str(v)
        else:
            try:
                json.dumps(v, default=str, sort_keys=True)
                norm[k] = v
            except (TypeError, ValueError):
                norm[k] = str(v)
    blob = json.dumps(norm, default=str, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


class MessageRefresher:
    """Debounced per-key message editor.

    schedule(key, builder, delay) — builder это async-функция,
    возвращающая EditRequest | None. None означает «нечего
    обновлять» (например, сообщение не найдено).
    """

    def __init__(
        self,
        default_delay: float = 1.5,
        max_keys: int = 2000,
        dead_seconds: float = 30.0,
    ) -> None:
        self._delay = default_delay
        self._max_keys = max_keys
        # После 404 ключ «мёртв» на dead_seconds: новые события
        # не должны немедленно перезапускать обречённый edit.
        # По истечении окно ключ снова живой — сообщение могло
        # быть пересоздано под тем же логическим id.
        self._dead_seconds = dead_seconds
        self._dead_until: dict[Any, float] = {}
        self._wanted: OrderedDict[Any, Builder] = OrderedDict()
        self._workers: dict[Any, asyncio.Task] = {}
        self._fingerprints: OrderedDict[Any, str] = OrderedDict()
        self._bg: set[asyncio.Task] = set()

    # ----------------------------------------------------------- public

    def schedule(
        self,
        key: Any,
        builder: Builder,
        delay: Optional[float] = None,
    ) -> None:
        """Запланировать обновление. Повторный вызов с тем же
        ключом просто заменяет builder — выполнится один проход
        с актуальным состоянием."""
        if self._worker_alive(key):
            self._wanted[key] = builder
            self._wanted.move_to_end(key)
            return
        dead = self._dead_until.get(key)
        if dead is not None:
            if dead > asyncio.get_event_loop().time():
                return  # сообщение удалено — не дёргаем API
            self._dead_until.pop(key, None)
        # Лимит очереди: выкидываем самый старый ожидающий ключ.
        while len(self._wanted) >= self._max_keys:
            old_key, _ = self._wanted.popitem(last=False)
            self._fingerprints.pop(old_key, None)
            logger.warning("refresher queue full — dropped key %r", old_key)
        self._wanted[key] = builder
        self._wanted.move_to_end(key)
        task = asyncio.create_task(self._worker(key, delay or self._delay))
        self._workers[key] = task
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def shutdown(self) -> None:
        """Отменить все воркеры (shutdown бота)."""
        for task in list(self._workers.values()):
            task.cancel()
        self._workers.clear()
        self._wanted.clear()
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)
            self._bg.clear()

    # --------------------------------------------------------- internal

    def _worker_alive(self, key: Any) -> bool:
        task = self._workers.get(key)
        return task is not None and not task.done()

    async def _worker(self, key: Any, delay: float) -> None:
        try:
            while True:
                await asyncio.sleep(delay)
                builder = self._wanted.pop(key, None)
                if builder is None:
                    return
                try:
                    await self._apply(key, builder)
                except Exception as exc:
                    logger.error(
                        "refresher apply %r failed: %s", key, exc,
                        exc_info=True,
                    )
                # Если за время edit прилетели новые schedule() —
                # _wanted снова заполнен, цикл сделает ещё проход.
        finally:
            # Между pop→None и выходом нет await — schedule()
            # не может вклиниться, потерянных правок нет.
            self._workers.pop(key, None)

    async def _apply(self, key: Any, builder: Builder) -> None:
        req = await builder()
        if req is None:
            return
        fp = payload_fingerprint(req.kwargs)
        if self._fingerprints.get(key) == fp:
            return  # содержимое не изменилось — PATCH не нужен
        try:
            await req.message.edit(**req.kwargs)
            self._fingerprints[key] = fp
            self._fingerprints.move_to_end(key)
            while len(self._fingerprints) > self._max_keys:
                self._fingerprints.popitem(last=False)
        except discord.NotFound:
            logger.info("refresher: message gone for key %r — stop", key)
            self._fingerprints.pop(key, None)
            self._dead_until[key] = (
                asyncio.get_event_loop().time() + self._dead_seconds
            )
            if req.on_missing is not None:
                try:
                    await req.on_missing()
                except Exception as exc:
                    logger.warning(
                        "refresher on_missing %r failed: %s", key, exc
                    )
        except discord.HTTPException as exc:
            if exc.status == 429:
                # HTTPClient уже ждёт retry_after; одна повторная
                # попытка после паузы — не больше.
                retry = getattr(exc, "retry_after", None) or 1.0
                await asyncio.sleep(min(float(retry), 5.0))
                try:
                    await req.message.edit(**req.kwargs)
                    self._fingerprints[key] = fp
                except discord.NotFound:
                    self._fingerprints.pop(key, None)
                    self._dead_until[key] = (
                        asyncio.get_event_loop().time()
                        + self._dead_seconds
                    )
                    if req.on_missing is not None:
                        try:
                            await req.on_missing()
                        except Exception:
                            pass
                except Exception as exc2:
                    logger.warning(
                        "refresher retry edit %r failed: %s", key, exc2
                    )
            else:
                logger.warning(
                    "refresher edit %r failed: %s", key, exc
                )


# ---------------------------------------------------------------------------
# Глобальный экземпляр для редко-настраиваемых вызовов
# ---------------------------------------------------------------------------

_default_refresher: Optional[MessageRefresher] = None


def get_refresher() -> MessageRefresher:
    global _default_refresher
    if _default_refresher is None:
        _default_refresher = MessageRefresher()
    return _default_refresher
