"""Парсер OCR-боксов экрана результатов CS 4x4 (Битва отрядов).

Раскладка скриншота: две панели команд (A слева, B справа),
в каждой 4 строки: ИМЯ | K/D/A | DMG.
"""

from __future__ import annotations

import re
from typing import Any

KDA_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})")
NUM_RE = re.compile(r"^\d{1,7}$")
NAME_CHAR_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
SKIP_WORDS = {
    "имя", "k/d/a", "dmg", "uma", "a", "b", "битва", "отрядов",
    "бермуда", "бермуды", "лобби", "conquest",
}


def _has_name_chars(text: str) -> bool:
    return bool(NAME_CHAR_RE.search(text))


def _is_kda_text(text: str) -> bool:
    return bool(KDA_RE.search(text))


def parse_cs_results(
    boxes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Возвращает {"team_a": [...], "team_b": [...], "top5": bool} или None."""
    # 1. Находим все K/D/A
    kda_matches = []
    for b in boxes:
        m = KDA_RE.search(b["text"])
        if m:
            kda_matches.append(
                {
                    "k": int(m.group(1)),
                    "d": int(m.group(2)),
                    "a": int(m.group(3)),
                    "box": b,
                    "mstart": m.start(),
                    "dmg_inline": _inline_dmg(b["text"], m.end()),
                }
            )
    if len(kda_matches) < 2:
        return None

    # 2. Делим на две команды по крупнейшему разрыву по X
    kda_matches.sort(key=lambda r: r["box"]["cx"])
    cxs = [r["box"]["cx"] for r in kda_matches]
    gaps = [cxs[i + 1] - cxs[i] for i in range(len(cxs) - 1)]
    split_idx = max(range(len(gaps)), key=lambda i: gaps[i])
    # разрыв должен быть реально большим (две колонки)
    if gaps[split_idx] < 80:
        return None
    left = kda_matches[: split_idx + 1]
    right = kda_matches[split_idx + 1 :]

    def _panel_players(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        players = []
        for row in sorted(rows, key=lambda r: r["box"]["cy"]):
            b = row["box"]
            tol = max(14.0, (b["y1"] - b["y0"]) * 0.8)

            # имя: текстовые боксы в той же строке левее K/D/A
            name_parts = []
            # + префикс того же бокса до K/D/A (OCR может склеить строку)
            prefix = b["text"][: row["mstart"]].strip()
            if prefix and _has_name_chars(prefix) and prefix.lower() not in SKIP_WORDS:
                name_parts.append((b["x0"], prefix))
            for nb in boxes:
                t = nb["text"]
                if nb is b or _is_kda_text(t) or NUM_RE.match(t):
                    continue
                if not _has_name_chars(t):
                    continue
                if t.lower() in SKIP_WORDS or len(t) < 2:
                    continue
                if abs(nb["cy"] - b["cy"]) > tol:
                    continue
                if nb["cx"] >= b["cx"] - 4:
                    continue
                name_parts.append((nb["cx"], t))
            name_parts.sort()
            # убираем дубли текста (OCR может дублировать)
            seen: set[str] = set()
            name = ""
            for _, t in name_parts:
                if t not in seen:
                    seen.add(t)
                    name = f"{name} {t}".strip()
            if not name:
                name = "???"

            # DMG: ближайший чисто-числовой бокс правее K/D/A в строке
            dmg = row["dmg_inline"]
            if dmg is None:
                best = None
                for nb in boxes:
                    if nb is b or not NUM_RE.match(nb["text"]):
                        continue
                    if abs(nb["cy"] - b["cy"]) > tol:
                        continue
                    if nb["cx"] <= b["cx"] + 4:
                        continue
                    if best is None or nb["cx"] < best["cx"]:
                        best = nb
                if best is not None:
                    try:
                        dmg = int(best["text"])
                    except ValueError:
                        dmg = None

            players.append(
                {
                    "name": name[:32],
                    "kills": row["k"],
                    "deaths": row["d"],
                    "assists": row["a"],
                    "dmg": dmg,
                    "kd": round(row["k"] / row["d"], 2) if row["d"] else row["k"],
                }
            )
        return players

    team_a = _panel_players(left)
    team_b = _panel_players(right)

    top5 = any(
        "ПЯТЕРКУ" in b["text"].upper() or "ЛУЧШИХ" in b["text"].upper()
        for b in boxes
    )
    return {"team_a": team_a, "team_b": team_b, "top5": top5}


def _inline_dmg(text: str, end_pos: int) -> int | None:
    """Число сразу после K/D/A в том же боксе ('5/6/3 263')."""
    rest = text[end_pos:].strip()
    m = re.match(r"^(\d{1,7})", rest)
    return int(m.group(1)) if m else None
