"""Строгий парсер индивидуальной статистики матча.

Единственный поддерживаемый формат — ``K/D`` (два целых
неотрицательных числа через ``/``)::

    7/4
    12/3
    0/5
    10/0

Всё остальное отклоняется: ``7/4/2``, ``7/4/2/1500``, отрицательные
значения, текст, неверные разделители.
"""

from __future__ import annotations

MAX_KD_VALUE = 99


def parse_kd(text: str) -> tuple[int, int] | None:
    """Разобрать строку ``K/D``.

    Возвращает ``(kills, deaths)`` или ``None`` при любой ошибке.
    """
    if not text:
        return None
    parts = text.strip().split("/")
    if len(parts) != 2:
        return None
    if not all(p.strip().isdigit() for p in parts):
        return None
    kills, deaths = int(parts[0]), int(parts[1])
    if kills > MAX_KD_VALUE or deaths > MAX_KD_VALUE:
        return None
    return kills, deaths


def parse_match_score(text: str) -> tuple[int, int] | None:
    """Разобрать счёт матча Kazakhstan (до 5 побед).

    Допустимые финальные счета: 5:0, 5:1, 5:2, 5:3, 5:4 и зеркально.
    Возвращает ``(score_winner, score_loser)`` или ``None``.
    """
    if not text:
        return None
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    if not all(p.strip().isdigit() for p in parts):
        return None
    a, b = int(parts[0]), int(parts[1])
    if max(a, b) != 5 or min(a, b) > 4:
        return None
    return max(a, b), min(a, b)
