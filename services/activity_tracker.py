"""Скользящий гейт активности для спонтанных событий 2.0.

Заменяет старый счётчик «100 сообщений + 30 минут».

Семантика (все времена — серверные, UTC, инъецируемые часы):

- Окно ``window_s`` (по умолчанию 300 c = 5 минут) скользящее:
  на каждое подходящее сообщение старые сэмплы вытесняются.
- Сообщение «засчитывается» (credited), только если:
  * автор не слал credited-сообщение последние ``min_interval_s``
    секунд (защита от флуда одним спамером);
  * у автора меньше ``max_per_author`` credited-сообщений в окне;
  * нормализованное содержимое не повторялось за ``dedup_ttl_s``
    (защита от копипасты/дублей).
- Событие может стартовать, когда в окне:
  * не менее ``min_messages`` credited-сообщений;
  * не менее ``min_unique_users`` разных авторов;
  * не менее ``min_active_authors`` разных авторов с credited-
    сообщением за последние 60 секунд (живой чат, а не старый хвост).

Трекер оценивается ТОЛЬКО на входящем подходящем сообщении —
сам по себе истёкший таймер событие не порождает. Сэмплы
собираются и во время кулдауна, поэтому первое сообщение после
кулдауна может сразу запустить событие.

Память ограничена: сэмплы старше окна и хэши старше dedup_ttl
вытесняются при каждой записи; жёсткий потолок ``max_samples``
на канал — лишние credited-сообщения отбрасываются (не теряя
уже учтённых).

Тела сообщений НЕ сохраняются — только unix-время, user_id и
хэш нормализованного содержимого.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import deque
from typing import Any, Hashable

_WS_RE = re.compile(r"\s+")


def normalize_content(text: str) -> str:
    """Нормализация для детекта дублей: lower + схлопнутые
    пробелы. Тело сообщения дальше не используется."""
    return _WS_RE.sub(" ", (text or "").strip().lower())


def content_hash(text: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(normalize_content(text).encode("utf-8"),
                        digest_size=8).digest(), "big")


class ActivityGate:
    """In-memory sliding-window трекер. Ключ — (guild_id, channel_id)."""

    DEFAULTS: dict[str, float] = {
        "window_s": 300.0,
        "min_messages": 10.0,
        "min_unique_users": 3.0,
        "max_per_author": 4.0,
        "min_interval_s": 10.0,
        "min_active_authors": 2.0,
        "dedup_ttl_s": 300.0,
        "max_samples": 5000.0,
    }

    def __init__(self, **overrides: float) -> None:
        cfg = dict(self.DEFAULTS)
        cfg.update({k: float(v) for k, v in overrides.items()
                    if k in cfg})
        self.window_s = cfg["window_s"]
        self.min_messages = int(cfg["min_messages"])
        self.min_unique_users = int(cfg["min_unique_users"])
        self.max_per_author = int(cfg["max_per_author"])
        self.min_interval_s = cfg["min_interval_s"]
        self.min_active_authors = int(cfg["min_active_authors"])
        self.dedup_ttl_s = cfg["dedup_ttl_s"]
        self.max_samples = int(cfg["max_samples"])
        # key -> {"samples": deque[(ts, uid, chash)],
        #         "last": {uid: ts}, "seen": deque[(ts, chash)]}
        self._channels: dict[Hashable, dict[str, Any]] = {}

    # ------------------------------------------------------------ internals

    def _state(self, key: Hashable) -> dict[str, Any]:
        st = self._channels.get(key)
        if st is None:
            st = {"samples": deque(), "last": {}, "seen": deque()}
            self._channels[key] = st
        return st

    def _evict(self, st: dict[str, Any], now: float) -> None:
        cut = now - self.window_s
        samples = st["samples"]
        while samples and samples[0][0] < cut:
            samples.popleft()
        seen = st["seen"]
        seen_cut = now - self.dedup_ttl_s
        while seen and seen[0][0] < seen_cut:
            seen.popleft()
        last = st["last"]
        for uid in [u for u, ts in last.items() if ts < cut]:
            del last[uid]

    # -------------------------------------------------------------- record

    def record(self, key: Hashable, user_id: int,
               text: str, now: float | None = None) -> bool:
        """Учесть сообщение. True — сообщение credited."""
        now = time.time() if now is None else now
        st = self._state(key)
        self._evict(st, now)

        last_ts = st["last"].get(user_id)
        if last_ts is not None and now - last_ts < self.min_interval_s:
            return False  # флуд: чаще min_interval_s

        chash = content_hash(text)
        seen_cut = now - self.dedup_ttl_s
        if any(h == chash for ts, h in st["seen"] if ts >= seen_cut):
            return False  # дубликат нормализованного контента

        per_author = sum(1 for _ts, uid, _h in st["samples"]
                         if uid == user_id)
        if per_author >= self.max_per_author:
            return False

        if len(st["samples"]) >= self.max_samples:
            return False  # потолок памяти канала

        st["samples"].append((now, user_id, chash))
        st["seen"].append((now, chash))
        st["last"][user_id] = now
        return True

    # ------------------------------------------------------------ evaluate

    def evaluate(self, key: Hashable,
                 now: float | None = None) -> bool:
        """Пороги окна выполнены? Оценивается только по уже
        записанным сэмплам — вызов сам по себе ничего не делает."""
        now = time.time() if now is None else now
        st = self._channels.get(key)
        if st is None:
            return False
        self._evict(st, now)
        samples = st["samples"]
        if len(samples) < self.min_messages:
            return False
        users = {uid for _ts, uid, _h in samples}
        if len(users) < self.min_unique_users:
            return False
        active_cut = now - 60.0
        active = {uid for ts, uid, _h in samples if ts >= active_cut}
        return len(active) >= self.min_active_authors

    def stats(self, key: Hashable,
              now: float | None = None) -> dict[str, int]:
        """Диагностика для тестов/логов (без тел сообщений)."""
        now = time.time() if now is None else now
        st = self._channels.get(key)
        if st is None:
            return {"samples": 0, "users": 0}
        self._evict(st, now)
        samples = st["samples"]
        return {
            "samples": len(samples),
            "users": len({uid for _ts, uid, _h in samples}),
        }

    def reset(self, key: Hashable | None = None) -> None:
        if key is None:
            self._channels.clear()
        else:
            self._channels.pop(key, None)
