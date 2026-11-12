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
SCORE_NUM_RE = re.compile(r"^\d{1,2}$")
VS_RE = re.compile(r"^(vs|v5|ws|vS)[.:]?$", re.IGNORECASE)
SCORE_GLUED_RE = re.compile(r"(\d{1,2})\s*[:：]?\s*(?:vs|v5|ws)\s*[:：]?\s*(\d{1,2})", re.IGNORECASE)
# «5:2» / «5-2» / «5|2» одним боксом — без букв VS (OCR мог склеить
# разделитель). Принимается только в шапке (выше строк K/D/A), чтобы
# не спутать с таймером или другими числами.
SCORE_PLAIN_RE = re.compile(r"^(\d{1,2})\s*[:：.\-–—|]\s*(\d{1,2})$")
NAME_CHAR_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")
SKIP_WORDS = {
    "имя", "k/d/a", "dmg", "uma", "a", "b", "битва", "отрядов",
    "бермуда", "бермуды", "лобби", "conquest",
    # Водяной знак бота на скриншотах результатов — не часть ника
    "r!z3", "!z3",
}
# Слова, которые не могут быть названием команды (шапка результата)
TEAMNAME_SKIP = SKIP_WORDS | {
    "победа", "поражение", "booyah", "byah", "victory", "defeat",
    "lost", "win", "lose", "пятерку", "лучших", "назад", "vs", "vs.",
    "топ", "mp", "вобин", "воях", "буя",
}
# Заголовки колонок таблицы — не название команды
HEADER_WORDS = {"имя", "name", "k/d/a", "dmg", "dm6", "dmg:", "dmg6", "dmg."}


def _has_name_chars(text: str) -> bool:
    return bool(NAME_CHAR_RE.search(text))


def _is_kda_text(text: str) -> bool:
    return bool(KDA_RE.search(text))


def parse_match_score(
    boxes: list[dict[str, Any]],
) -> tuple[int, int] | None:
    """Счёт матча «N VS M» в шапке экрана (выигранные раунды).

    Возвращает (score_left, score_right) — левой/правой команды
    на экране (= team A / team B парсера). None если не найден.
    """
    # 1) Отдельные боксы: число | VS | число в одной строке
    for vb in boxes:
        if not VS_RE.match(vb["text"].strip()):
            continue
        tol = 20.0
        left = [
            b for b in boxes
            if b is not vb and SCORE_NUM_RE.match(b["text"].strip())
            and b["cx"] < vb["cx"] - 15 and abs(b["cy"] - vb["cy"]) <= tol
        ]
        right = [
            b for b in boxes
            if b is not vb and SCORE_NUM_RE.match(b["text"].strip())
            and b["cx"] > vb["cx"] + 15 and abs(b["cy"] - vb["cy"]) <= tol
        ]
        if not left or not right:
            continue
        l = min(left, key=lambda b: vb["cx"] - b["cx"])
        r = min(right, key=lambda b: b["cx"] - vb["cx"])
        try:
            a, c = int(l["text"]), int(r["text"])
        except ValueError:
            continue
        if 0 <= a <= 30 and 0 <= c <= 30 and (a or c):
            return (a, c)

    # 2) Склеенный вариант в одном боксе: "5:2", "5 VS 2", "5vs2"
    for b in boxes:
        m = SCORE_GLUED_RE.search(b["text"])
        if m:
            a, c = int(m.group(1)), int(m.group(2))
            if 0 <= a <= 30 and 0 <= c <= 30 and (a or c):
                return (a, c)

    # 3) «N:M» / «N-M» / «N | M» одним боксом без букв VS.
    # Только выше строк K/D/A — иначе «12:30» (таймер) даст ложный счёт.
    kda_cys = [b["cy"] for b in boxes if KDA_RE.search(b["text"])]
    top_limit = min(kda_cys) if kda_cys else float("inf")
    for b in boxes:
        m = SCORE_PLAIN_RE.match(b["text"].strip())
        if not m or b["cy"] >= top_limit:
            continue
        a, c = int(m.group(1)), int(m.group(2))
        if 0 <= a <= 30 and 0 <= c <= 30 and (a or c):
            return (a, c)
    return None


def parse_cs_results(
    boxes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Возвращает {"team_a": [...], "team_b": [...], "top5": bool,
    "score": (a, b)|None, "banner": ...} или None."""
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

    # 2. Делим на две команды по крупнейшему разрыву по X.
    # Порог относительный (доля ширины кадра), а не абсолютные 80px —
    # иначе на скриншотах ~1280px колонки не разделялись.
    kda_matches.sort(key=lambda r: r["box"]["cx"])
    cxs = [r["box"]["cx"] for r in kda_matches]
    gaps = [cxs[i + 1] - cxs[i] for i in range(len(cxs) - 1)]
    split_idx = max(range(len(gaps)), key=lambda i: gaps[i])
    img_w = max((b["x1"] for b in boxes), default=0.0)
    min_gap = max(30.0, img_w * 0.03)
    if gaps[split_idx] < min_gap:
        return None
    left = kda_matches[: split_idx + 1]
    right = kda_matches[split_idx + 1 :]

    # Граница между панелями — чтобы имена одной команды не подтягивались
    # в строки другой (у правой панели K/D/A правее, поэтому без границы
    # она захватывала имена левой команды)
    boundary_x = (cxs[split_idx] + cxs[split_idx + 1]) / 2

    def _pad_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Гарантировать 4 позиции строк на панель CS 4×4.

        K/D/A — якорь детекции, но строка игрока существует и при
        нераспознанном K/D/A или нике: оцениваем шаг строк по cy
        и достраиваем недостающие слоты (поля = None, не 0 —
        статистику заполнит ручное исправление/маппинг).
        """
        rows = sorted(rows, key=lambda r: r["box"]["cy"])
        if len(rows) >= 4:
            return rows[:4]
        if len(rows) < 2:
            return rows
        cys = [r["box"]["cy"] for r in rows]
        diffs = [cys[i + 1] - cys[i] for i in range(len(cys) - 1)]
        pitch = sorted(diffs)[len(diffs) // 2]
        if pitch <= 0:
            return rows
        # Якорь сетки: top = cy_j - j*pitch для каждой строки j —
        # выбираем вариант с минимальным суммарным отклонением.
        best_top, best_err = None, float("inf")
        for j, cy in enumerate(cys):
            top = cy - j * pitch
            err = sum(
                min(abs(cy2 - (top + i * pitch)) for i in range(4))
                for cy2 in cys
            )
            if err < best_err:
                best_err, best_top = err, top
        have = {round((r["box"]["cy"] - best_top) / pitch) for r in rows}
        have = {i for i in have if 0 <= i < 4}
        panel_cx = sum(r["box"]["cx"] for r in rows) / len(rows)
        x0 = min(r["box"]["x0"] for r in rows)
        x1 = max(r["box"]["x1"] for r in rows)
        y_h = max((r["box"]["y1"] - r["box"]["y0"] for r in rows), default=pitch * 0.6)
        out = list(rows)
        for i in range(4):
            if i in have:
                continue
            cy = best_top + i * pitch
            out.append({
                "k": None, "d": None, "a": None,
                "mstart": 0, "dmg_inline": None,
                "box": {
                    "text": "", "cx": panel_cx, "cy": cy,
                    "x0": x0, "x1": x1,
                    "y0": cy - y_h / 2, "y1": cy + y_h / 2,
                },
            })
        return sorted(out, key=lambda r: r["box"]["cy"])[:4]

    left = _pad_rows(left)
    right = _pad_rows(right)

    def _panel_players(
        rows: list[dict[str, Any]], side: str
    ) -> list[dict[str, Any]]:
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
                # Бокс должен быть на стороне своей панели
                if side == "left" and nb["cx"] >= boundary_x:
                    continue
                if side == "right" and nb["cx"] <= boundary_x:
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
                    "name": name[:32] if name != "???" else "???",
                    "kills": row["k"],
                    "deaths": row["d"],
                    "assists": row["a"],
                    "dmg": dmg,
                    "kd": (
                        round(row["k"] / row["d"], 2)
                        if row["k"] is not None and row["d"]
                        else (float(row["k"]) if row["k"] is not None else None)
                    ),
                    "cy": b["cy"],
                    "row_h": max(8.0, b["y1"] - b["y0"]),
                }
            )
        return players

    max_x = max(b["x1"] for b in boxes)

    def _panel_name(
        rows: list[dict[str, Any]], side: str
    ) -> tuple[str | None, tuple[float, float, float, float] | None]:
        """Название команды из шапки панели (РАЗРУШИТЕЛИ, ХИЩНИКИ и т.п.).

        Ищем текст над строкой заголовков колонок ("имя"/"K/D/A"/"DMG"),
        внутри x-диапазона своей панели.

        Возвращает (имя, bbox области имени) — bbox нужен для повторного
        OCR кириллической моделью (основной rec-чарсет не знает кириллицу
        и выдаёт латиницу-кракозябры вместо «ХИЩНИКИ»).
        """
        if not rows:
            return None, None
        cy_sorted = sorted(r["box"]["cy"] for r in rows)
        diffs = [cy_sorted[i + 1] - cy_sorted[i] for i in range(len(cy_sorted) - 1)]
        pitch = max(sorted(diffs)[len(diffs) // 2] if diffs else 90.0, 30.0)
        margin = max_x * 0.02
        if side == "left":
            x_lo, x_hi = 0.0, boundary_x - margin
        else:
            x_lo, x_hi = boundary_x + margin, max_x

        def _in_panel(b: dict[str, Any]) -> bool:
            return x_lo <= b["cx"] <= x_hi

        # Y-окно: строка с названием команды — прямо над заголовками колонок
        header_cy = min(
            (
                b["cy"]
                for b in boxes
                if _in_panel(b)
                and b["text"].strip().lower().rstrip(":.") in HEADER_WORDS
            ),
            default=None,
        )
        if header_cy is not None:
            y_lo, y_hi = header_cy - 3.0 * pitch, header_cy - 10
        else:
            top_cy = cy_sorted[0]
            y_lo, y_hi = top_cy - 2.6 * pitch, top_cy - 0.55 * pitch

        cands = []
        for b in boxes:
            t = b["text"].strip()
            if not _in_panel(b) or not (y_lo < b["cy"] < y_hi):
                continue
            if not _has_name_chars(t) or len(t) < 2:
                continue
            if t.lower() in TEAMNAME_SKIP or _is_kda_text(t) or NUM_RE.match(t):
                continue
            cands.append(b)
        if not cands:
            return None, None
        # Берём кластер строк ближе всего к заголовкам (название команды)
        cands.sort(key=lambda b: b["cy"])
        clusters: list[list[dict[str, Any]]] = [[cands[0]]]
        for b in cands[1:]:
            if b["cy"] - clusters[-1][-1]["cy"] <= 0.45 * pitch:
                clusters[-1].append(b)
            else:
                clusters.append([b])
        best = max(clusters, key=lambda cl: sum(b["cy"] for b in cl) / len(cl))
        best.sort(key=lambda b: b["x0"])
        name = " ".join(b["text"].strip() for b in best)
        if not name:
            return None, None
        bbox = (
            min(b["x0"] for b in best),
            min(b["y0"] for b in best),
            max(b["x1"] for b in best),
            max(b["y1"] for b in best),
        )
        return name[:48], bbox

    team_a = _panel_players(left, "left")
    team_b = _panel_players(right, "right")
    name_a, box_a = _panel_name(left, "left")
    name_b, box_b = _panel_name(right, "right")

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
        "team_a_name": name_a,
        "team_b_name": name_b,
        "team_a_name_box": box_a,
        "team_b_name_box": box_b,
        "top5": top5,
        "banner": banner,
        "score": parse_match_score(boxes),
        # Геометрия для targeted re-OCR строк: граница панелей и
        # x-диапазоны сторон (координаты исходного изображения).
        "boundary_x": boundary_x,
        "img_w": img_w,
        "img_h": max((b["y1"] for b in boxes), default=0.0),
    }


def parse_row_fields(
    boxes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Поля одной строки игрока из боксов узкой полосы (targeted OCR).

    Возвращает только распознанное: name/kills/deaths/assists/dmg —
    отсутствующее не выдумываем (None), 0 подставляет вызывающий код
    только после ручного подтверждения.
    """
    out: dict[str, Any] = {}
    kda_box = None
    for b in boxes:
        gm = KDA_DMG_GLUED_RE.search(b["text"])
        if gm:
            kda_box = b
            out.update(
                kills=int(gm.group(1)), deaths=int(gm.group(2)),
                assists=int(gm.group(3)),
            )
            rest = b["text"][gm.end():].strip()
            dm = re.match(r"^(\d{3,7})", rest)
            if dm:
                out["dmg"] = int(dm.group(1))
            break
        m = KDA_RE.search(b["text"])
        if m:
            kda_box = b
            out.update(
                kills=int(m.group(1)), deaths=int(m.group(2)),
                assists=int(m.group(3)),
            )
            break
    if kda_box is not None:
        # DMG — ближайшее число правее K/D/A в полосе
        best = None
        for b in boxes:
            if b is kda_box or not NUM_RE.match(b["text"].strip()):
                continue
            if b["cx"] <= kda_box["cx"]:
                continue
            if best is None or b["cx"] < best["cx"]:
                best = b
        if best is not None:
            try:
                out["dmg"] = int(best["text"])
            except ValueError:
                pass
    # Имя — текстовые боксы с буквами, не K/D/A и не чистые числа
    name_parts = []
    for b in boxes:
        t = b["text"].strip()
        if not t or _is_kda_text(t) or NUM_RE.match(t):
            continue
        if not _has_name_chars(t) or t.lower() in SKIP_WORDS or len(t) < 2:
            continue
        if kda_box is not None and b["cx"] >= kda_box["cx"] - 4:
            continue
        name_parts.append((b["cx"], t))
    name_parts.sort()
    seen: set[str] = set()
    name = ""
    for _, t in name_parts:
        if t not in seen:
            seen.add(t)
            name = f"{name} {t}".strip()
    if name:
        out["name"] = name[:32]
    return out


def _inline_dmg(text: str, end_pos: int) -> int | None:
    """Число сразу после K/D/A в том же боксе ('5/6/3 263')."""
    rest = text[end_pos:].strip()
    m = re.match(r"^(\d{1,7})", rest)
    return int(m.group(1)) if m else None
