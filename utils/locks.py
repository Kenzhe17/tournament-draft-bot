"""Общие примитивы блокировок для OCR/турнирных операций."""

from __future__ import annotations

import asyncio


class TrackedLock(asyncio.Lock):
    """asyncio.Lock с подсчётом задач внутри acquire().

    locked() == False НЕ гарантирует отсутствие waiter'ов: после
    release() пробуждённый waiter ещё не успел захватить лок, а
    лок в этот момент unlocked. _pending считает все задачи,
    находящиеся внутри acquire() — и ожидающие, и только что
    захватившие. Вытеснять лок безопасно только когда
    ``not locked() and _pending == 0``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending = 0

    async def acquire(self):  # noqa: D102 - proxy to super
        self._pending += 1
        try:
            return await super().acquire()
        finally:
            self._pending -= 1


def lock_is_idle(lock) -> bool:
    """Лок можно вытеснить: не удерживается и нет ожидающих."""
    if lock is None or lock.locked():
        return False
    # Обычный asyncio.Lock без _pending — считаем idle, если свободен;
    # все рабочие локи создаются как TrackedLock.
    return getattr(lock, "_pending", 0) == 0
