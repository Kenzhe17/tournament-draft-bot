"""Парсер OCR-боксов экрана результатов CS 4x4 (Битва отрядов).

Раскладка скриншота: две панели команд (A слева, B справа),
в каждой 4 строки: ИМЯ | K/D/A | DMG.
"""

from __future__ import annotations

import re
from typing import Any

KDA_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})")
# K/D/A + DMG склеены без пробела: "12/2/1457" → k=12 d=2 a=1 dmg=457
# (ассист всегда 1 цифра, урон 3+ цифр)
KDA_DMG_GLUED_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d)\s*(\d{3,7})")
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


def _pick_nick_parts(
    boxes: list[dict[str, Any]],
    kda_box: dict[str, Any],
    side: str,
    boundary_x: float,
    tol: float,
    prefix: str,
) -> list[tuple[float, str]]:
    """Собирает части имени игрока из OCR-боксов строки K/D/A.

    Ячейка игрока в результатах FF содержит две текстовые строки:
    ник (крупный шрифт, сверху) и название гильдии (мельче, снизу).
    Простое соседство по Y захватывает обе — поэтому кандидаты
    кластеризуются в строки, а выбирается строка ника: с наибольшей
    высотой бокса (крупный шрифт), при равенстве — верхняя.
    """
    name_tol = tol * 1.8  # шире обычного: нужно захватить и строку гильдии
    cands: list[tuple[float, str, float, float]] = []  # cx, text, cy, height
    for nb in boxes:
        t = nb["text"]
        if nb is kda_box or _is_kda_text(t) or NUM_RE.match(t):
            continue
        if not _has_name_chars(t):
            continue
        if t.lower() in SKIP_WORDS or len(t) < 2:
            continue
        if abs(nb["cy"] - kda_box["cy"]) > name_tol:
            continue
        if nb["cx"] >= kda_box["cx"] - 4:
            continue
        # Бокс должен быть на стороне своей панели
        if side == "left" and nb["cx"] >= boundary_x:
            continue
        if side == "right" and nb["cx"] <= boundary_x:
            continue
        cands.append((nb["cx"], t, nb["cy"], nb["y1"] - nb["y0"]))

    prefix_ok = (
        bool(prefix)
        and _has_name_chars(prefix)
        and prefix.lower() not in SKIP_WORDS
    )
    if not cands:
        return [(kda_box["x0"], prefix)] if prefix_ok else []

    # Кластеризация по вертикали: cy ближе 15px — одна строка
    cands.sort(key=lambda c: c[2])
    lines: list[list[tuple[float, str, float, float]]] = []
    for c in cands:
        if lines and c[2] - lines[-1][-1][2] <= 15.0:
            lines[-1].append(c)
        else:
            lines.append([c])

    def _score(line: list[tuple[float, str, float, float]]):
        max_h = max(h for _, _, _, h in line)
        mean_cy = sum(cy for _, _, cy, _ in line) / len(line)
        # крупный шрифт ника важнее; при равной высоте — верхняя строка
        return (max_h, -mean_cy)

    best = max(lines, key=_score)
    best.sort(key=lambda c: c[0])
    parts = [(cx, t) for cx, t, _, _ in best]
    if prefix_ok:
        parts.insert(0, (kda_box["x0"], prefix))
    return parts


def parse_cs_results(
    boxes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Возвращает {"team_a": [...], "team_b": [...], "top5": bool} или None."""
    # 1. Находим все K/D/A (включая склеенные с уроном "12/2/1457")
    kda_matches = []
    for b in boxes:
        gm = KDA_DMG_GLUED_RE.search(b["text"])
        if gm:
            kda_matches.append(
                {
                    "k": int(gm.group(1)),
                    "d": int(gm.group(2)),
                    "a": int(gm.group(3)),
                    "box": b,
                    "mstart": gm.start(),
                    "dmg_inline": int(gm.group(4)),
                }
            )
            continue
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

    # Граница между панелями — чтобы имена одной команды не подтягивались
    # в строки другой (у правой панели K/D/A правее, поэтому без границы
    # она захватывала имена левой команды)
    boundary_x = (cxs[split_idx] + cxs[split_idx + 1]) / 2

    def _panel_players(
        rows: list[dict[str, Any]], side: str
    ) -> list[dict[str, Any]]:
        players = []
        for row in sorted(rows, key=lambda r: r["box"]["cy"]):
            b = row["box"]
            tol = max(14.0, (b["y1"] - b["y0"]) * 0.8)

            # имя: текстовые боксы в той же строке левее K/D/A.
            # В FF-результатах ячейка игрока содержит ДВЕ строки:
            # ник (крупный шрифт) и название гильдии (мельче, ниже).
            # Собираем кандидатов в расширенном окне, кластеризуем по
            # строкам и оставляем только строку ника.
            prefix = b["text"][: row["mstart"]].strip()
            name_parts = _pick_nick_parts(
                boxes, b, side, boundary_x, tol, prefix
            )
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

    team_a = _panel_players(left, "left")
    team_b = _panel_players(right, "right")

    top5 = any(
        "ПЯТЕРКУ" in b["text"].upper() or "ЛУЧШИХ" in b["text"].upper()
        for b in boxes
    )

    # Баннер результата матча для команды игрока
    banner = None
    all_text = " ".join(b["text"].upper() for b in boxes)
    if "ПОРАЖЕНИЕ" in all_text or "DEFEAT" in all_text or "LOST" in all_text:
        banner = "lose"
    elif (
        "ПОБЕДА" in all_text
        or "BOOYAH" in all_text
        or "BYAH" in all_text
        or "VICTORY" in all_text
    ):
        banner = "win"

    return {
        "team_a": team_a,
        "team_b": team_b,
        "top5": top5,
        "banner": banner,
    }


def _inline_dmg(text: str, end_pos: int) -> int | None:
    """Число сразу после K/D/A в том же боксе ('5/6/3 263')."""
    rest = text[end_pos:].strip()
    m = re.match(r"^(\d{1,7})", rest)
    return int(m.group(1)) if m else None
