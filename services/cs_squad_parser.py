"""Парсер OCR-боксов детального экрана результатов CS — ОДИН отряд.

Раскладка: одна таблица из 4 строк (отряд игрока).
Колонки: ИМЯ | K/D/A | DMG | ФАКТИЧЕСКИЙ УРОН | НОКДАУНЫ |
ИСЦЕЛЕНИЕ | ПОДНЯТИЯ (2 подколонки) | ХЭДШОТЫ %.

Баннер ПОБЕДА/ПОРАЖЕНИЕ/ВОБИН(BOOYAH) показывает результат ЭТОГО отряда.
Позиции колонок заданы долями от ширины изображения — layout
масштабируется (1920/2556/2796 px).
"""

from __future__ import annotations

import re
from typing import Any

from services.cs_parser import KDA_RE, NUM_RE, _has_name_chars

# Признаки детального экрана (отличие от классического двух-панельного)
SQUAD_HEADER_RE = re.compile(
    r"нокдаун|исцелен|фактическ|хэдшот|headshot|поднят|поднять|"
    r"knockdown|heal|revive",
    re.IGNORECASE,
)
VALUE_RE = re.compile(r"^(\d{1,7})(?:[.,]\d+)?%?$")
SKIP_WORDS = {
    "имя", "name", "k/d/a", "dmg", "dm6", "dmg6", "dmg.", "лобби",
    "победа", "поражение", "booyah", "byah", "вобин", "воях", "буя",
    "назад", "битва", "отрядов", "conquest", "vs", "топ",
}

# Границы колонок — доли от ширины изображения (по max x1)
# Центры колонок в референсе 2796px: dmg~0.33, fact~0.43, knock~0.52,
# heal~0.63, rev1~0.72, rev2~0.77, hs~0.84
COL_WINDOWS = {
    "dmg": (0.285, 0.375),
    "fact_dmg": (0.375, 0.475),
    "knockdowns": (0.475, 0.575),
    "healing": (0.575, 0.70),
    "revives": (0.70, 0.745),
    "revive_x": (0.745, 0.80),
    "headshots": (0.80, 0.93),
}


def looks_like_squad_screen(boxes: list[dict[str, Any]]) -> bool:
    """True, если это детальный экран одного отряда (а не 2 панели)."""
    for b in boxes:
        if SQUAD_HEADER_RE.search(b["text"]):
            return True
    # Запасной вариант: ровно одна колонка K/D/A + широкие стат-колонки
    kda_cols = [
        b for b in boxes
        if KDA_RE.search(b["text"]) and len(KDA_RE.findall(b["text"])) == 1
    ]
    if len(kda_cols) < 3:
        return False
    cxs = sorted(b["cx"] for b in kda_cols)
    # одна колонка — разброс cx маленький относительно ширины
    width = max(b["x1"] for b in boxes) - min(b["x0"] for b in boxes)
    if cxs[-1] - cxs[0] > width * 0.15:
        return False
    # есть числа правее K/D/A (стат-колонки)
    right_nums = [
        b for b in boxes
        if NUM_RE.match(b["text"])
        and b["cx"] > cxs[-1] + width * 0.08
    ]
    return len(right_nums) >= 6


def _col_value(
    boxes: list[dict[str, Any]],
    row_cy: float,
    tol: float,
    window: tuple[float, float],
    width: float,
) -> float | None:
    """Число в колонке [x0_frac, x1_frac] на строке row_cy."""
    best = None
    for b in boxes:
        m = VALUE_RE.match(b["text"].strip())
        if not m:
            continue
        frac = b["cx"] / width if width else 0
        if not (window[0] <= frac <= window[1]):
            continue
        if abs(b["cy"] - row_cy) > tol:
            continue
        if best is None or abs(b["cy"] - row_cy) < abs(best["cy"] - row_cy):
            best = b
    if best is None:
        return None
    try:
        return float(best["text"].rstrip("%").replace(",", "."))
    except ValueError:
        return None


def _row_name(
    boxes: list[dict[str, Any]],
    row_cy: float,
    tol: float,
    kda_cx: float,
    width: float,
) -> str:
    """Имя игрока — текстовые боксы в строке левее K/D/A."""
    parts = []
    for nb in boxes:
        t = nb["text"].strip()
        if not _has_name_chars(t) or len(t) < 2:
            continue
        if t.lower() in SKIP_WORDS or NUM_RE.match(t) or KDA_RE.search(t):
            continue
        if abs(nb["cy"] - row_cy) > tol:
            continue
        if nb["cx"] >= kda_cx - width * 0.005:
            continue
        # имя не должно уходить слишком далеко влево (левое UI)
        if nb["cx"] < width * 0.04:
            continue
        parts.append((nb["cx"], t))
    parts.sort()
    seen: set[str] = set()
    name = ""
    for _, t in parts:
        if t not in seen:
            seen.add(t)
            name = f"{name} {t}".strip()
    return name[:32] or "???"


def parse_squad_results(
    boxes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Распарсить детальный экран одного отряда.

    Возвращает {"players": [...], "banner": "win"|"lose"|None} или None.
    """
    if not looks_like_squad_screen(boxes):
        return None

    # Строки = боксы K/D/A (одна колонка)
    kda_rows = []
    for b in boxes:
        m = KDA_RE.search(b["text"])
        if not m:
            continue
        kda_rows.append(
            {
                "k": int(m.group(1)),
                "d": int(m.group(2)),
                "a": int(m.group(3)),
                "box": b,
            }
        )
    if not (3 <= len(kda_rows) <= 6):
        return None

    width = max(b["x1"] for b in boxes) or 1.0
    kda_rows.sort(key=lambda r: r["box"]["cy"])
    # Одна колонка K/D/A — cx должны быть близки
    cxs = [r["box"]["cx"] for r in kda_rows]
    if max(cxs) - min(cxs) > width * 0.15:
        return None

    players = []
    for row in kda_rows:
        b = row["box"]
        tol = max(16.0, (b["y1"] - b["y0"]) * 1.2)
        p = {
            "name": _row_name(boxes, b["cy"], tol, b["cx"], width),
            "kills": row["k"],
            "deaths": row["d"],
            "assists": row["a"],
        }
        for col, win in COL_WINDOWS.items():
            v = _col_value(boxes, b["cy"], tol, win, width)
            if col == "headshots" and v is not None:
                v = round(v, 1)
            p[col] = int(v) if v is not None and col != "headshots" else v
        players.append(p)

    if len(players) < 3:
        return None

    # Баннер результата отряда
    all_text = " ".join(b["text"].upper() for b in boxes)
    banner = None
    if (
        "ПОРАЖЕНИЕ" in all_text or "DEFEAT" in all_text
        or "LOST" in all_text or "ПРОИГРЫШ" in all_text
    ):
        banner = "lose"
    elif (
        "ПОБЕДА" in all_text or "BOOYAH" in all_text
        or "BYAH" in all_text or "ВОБИН" in all_text
        or "ВОЯХ" in all_text or "VICTORY" in all_text
    ):
        banner = "win"

    return {"players": players, "banner": banner}
